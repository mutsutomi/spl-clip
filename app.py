#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_clip.py のブラウザ操作版。

起動:
  streamlit run app.py
  (または 起動.command をダブルクリック)

検出ロジックは auto_clip.py をそのまま呼んでいるので、コマンド版と結果は同じになる。
"""

import base64
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import streamlit as st

import auto_clip as ac

VIDEO_EXTS = [".mov", ".mp4", ".mkv", ".avi", ".m4v"]
WIN_SEC = ac.CONFIG["win_sec"]
OUT_DIR = Path(ac.CONFIG["out_dir"])
CACHE_DIR = Path(ac.CONFIG["cache_dir"])
MAX_POINTS = 1500  # グラフに描く点の上限(これを超えたら間引く)
PLOT_H = 240       # グラフ本体の高さ(px)

# 検証済みパレット(dataviz)。Streamlit の実際の背景色に対して検査済み
THEMES = {
    "light": {
        "series": "#2a78d6", "ink": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "surface": "#ffffff", "band_opacity": 0.10,
    },
    "dark": {
        "series": "#3987e5", "ink": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "surface": "#0e1117", "band_opacity": 0.18,
    },
}


def theme() -> dict:
    try:
        return THEMES["dark" if st.context.theme.type == "dark" else "light"]
    except Exception:
        return THEMES["light"]


def pick_file_dialog() -> str | None:
    """OSの「ファイルを選ぶ」ダイアログを開いてパスを返す(キャンセル時は None)。

    ダイアログはこのアプリを動かしているPC上に表示される。
    """
    if sys.platform == "darwin":
        types = ", ".join(f'"{e.lstrip(".")}"' for e in VIDEO_EXTS)
        script = f'POSIX path of (choose file with prompt "動画を選んでください" of type {{{types}}})'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    elif sys.platform.startswith("win"):
        pattern = ";".join(f"*{e}" for e in VIDEO_EXTS)
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.OpenFileDialog;"
            f"$d.Filter = '動画ファイル|{pattern}|すべてのファイル|*.*';"
            "$d.Title = '動画を選んでください';"
            "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.FileName) }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True,
        )
    else:
        return None  # Linux などはパス入力欄を使ってもらう
    return r.stdout.strip() or None


def default_video() -> str:
    """初期値として、このフォルダで一番新しい動画を選んでおく"""
    found = [p for p in Path(".").iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    if not found:
        return ""
    return str(max(found, key=lambda p: p.stat().st_mtime).resolve())


def ensure_audio(video: Path, track: int) -> Path:
    """検出用WAVを用意する。トラックを変えたときだけ自動で取り込み直す"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wav = ac.cache_wav_path(video, CACHE_DIR)
    key = (str(video.resolve()), track)

    # 既にあるWAVがどのトラックのものか分からない初回は、そのまま信用して使う
    stale = st.session_state.get("audio_key") not in (None, key)
    if stale or not wav.exists():
        with st.spinner("音声を取り込んでいます…(長い動画では数分かかります)"):
            ac.extract_audio(video, wav, track, refresh=True)
    st.session_state["audio_key"] = key
    return wav


@st.cache_data(show_spinner=False)
def load_tracks(video_path: str, mtime: float):
    """動画の音声トラック一覧。スライダー操作のたびにffprobeを呼ばないようキャッシュする"""
    return ac.audio_tracks(Path(video_path))


@st.cache_data(show_spinner=False)
def load_rms(wav_path: str, mtime: float, win_sec: float):
    """音量の解析結果をキャッシュする。スライダー操作のたびに再計算しないための要"""
    return ac.compute_rms(Path(wav_path), win_sec)


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def delete_cache(paths: list[Path]):
    for p in paths:
        try:
            p.unlink()
        except OSError as e:
            st.warning(f"{p.name} を削除できませんでした: {e}")
    load_rms.clear()     # 解析結果のキャッシュも一緒に捨てる
    load_sprite.clear()
    st.rerun()


def render_cache_panel(current_video: Path | None = None):
    """溜まった作業ファイルの量を見せて、その場で削除できるようにする"""
    files = sorted(
        (p for p in CACHE_DIR.iterdir() if p.suffix in (".wav", ".jpg")),
        key=lambda p: p.stat().st_mtime, reverse=True,
    ) if CACHE_DIR.exists() else []
    total = sum(p.stat().st_size for p in files)
    in_use = {ac.cache_wav_path(current_video, CACHE_DIR),
              ac.sprite_path(current_video, CACHE_DIR)} if current_video else set()

    with st.expander(f"🗂 作業ファイル(キャッシュ) — {len(files)} 個 / 合計 {fmt_size(total)}"):
        st.caption(
            "音量を調べるために取り出した音声と、プレビュー用のコマ画像です。"
            "音声は動画1時間あたり約110MBあり、自動では消えません。"
            "消しても次に同じ動画を開いたときに作り直されます"
            "(そのぶん待ち時間が増えるだけです)。"
        )
        if not files:
            st.info("作業ファイルはありません。")
            return

        st.dataframe(
            [
                {
                    "": "● 使用中" if p in in_use else "",
                    "元の動画": p.name.rsplit("_", 2)[0],
                    "種類": "音声" if p.suffix == ".wav" else "コマ画像",
                    "サイズ": fmt_size(p.stat().st_size),
                    "最終使用": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                }
                for p in files
            ],
            width="stretch",
            hide_index=True,
        )

        others = [p for p in files if p not in in_use]
        c1, c2 = st.columns(2)
        if c1.button(
            f"今使っていない {len(others)} 個を削除", disabled=not others,
            help="いま開いている動画のぶんは残すので、待ち時間は発生しません",
        ):
            delete_cache(others)
        with c2.popover("すべて削除", width="stretch"):
            st.write(
                f"{len(files)} 個({fmt_size(total)})をすべて削除します。"
                "いま開いている動画の音声も取り込み直しになります。"
            )
            if st.button("削除する", type="primary"):
                delete_cache(files)


def thin_out(rms: np.ndarray, win_sec: float) -> tuple[list, list]:
    """描画用に点を間引く。区間の最大値を残すので、山(ピーク)は消えない"""
    idx = np.arange(len(rms))
    if len(rms) > MAX_POINTS:
        k = int(np.ceil(len(rms) / MAX_POINTS))
        n = len(rms) // k
        block = rms[: n * k].reshape(n, k)
        idx = block.argmax(axis=1) + np.arange(n) * k
    return (idx * win_sec).round(2).tolist(), rms[idx].round(1).tolist()


@st.cache_data(show_spinner=False)
def load_sprite(video_path: str, mtime: float, duration: float):
    """プレビュー用サムネイルを用意する。作れなかった場合は None(グラフは出る)"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    video = Path(video_path)
    out = ac.sprite_path(video, CACHE_DIR)
    if not out.exists() and ac.make_sprite(video, out, duration) is None:
        return None
    size = ac.probe_size(out)
    if size is None:
        return None
    meta = ac.sprite_meta(duration, size)
    meta["uri"] = "data:image/jpeg;base64," + base64.b64encode(out.read_bytes()).decode()
    return meta


CHART_TEMPLATE = """
<meta charset="utf-8">
<style>
  body { margin: 0; background: transparent;
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  #band { position: relative; }
  #hint { color: var(--muted); font-size: 12px; padding: 6px 2px; }
  #shot { position: absolute; top: 0; left: 0; display: none;
          border-radius: 4px; box-shadow: 0 1px 6px rgba(0,0,0,.28);
          background-repeat: no-repeat; }
  #chip { position: absolute; left: 6px; bottom: 6px; padding: 2px 6px;
          border-radius: 3px; background: rgba(0,0,0,.72); color: #fff;
          font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
  svg { display: block; }
  .lbl { fill: var(--muted); font-size: 11px; }
</style>
<div id="root">
  <div id="band"><div id="hint"></div><div id="shot"><div id="chip"></div></div></div>
  <div id="plot"></div>
</div>
<script>
const D = __DATA__;
const root = document.getElementById("root");
const band = document.getElementById("band");
const hint = document.getElementById("hint");
const shot = document.getElementById("shot");
const chip = document.getElementById("chip");
const plot = document.getElementById("plot");
const C = D.colors;
root.style.setProperty("--muted", C.muted);

const PAD = { l: 8, r: 8, t: 10, b: 22 };
const step = D.t.length > 1 ? D.t[1] - D.t[0] : 1;
const vmax = Math.max(Math.max.apply(null, D.v), D.threshold) * 1.06 || 1;
const bandH = D.sprite ? D.sprite.tile_h : 0;
band.style.height = (D.sprite ? bandH : 26) + "px";

if (D.sprite) {
  shot.style.width = D.sprite.tile_w + "px";
  shot.style.height = D.sprite.tile_h + "px";
  shot.style.backgroundImage = "url(" + D.sprite.uri + ")";
  hint.textContent = "グラフにカーソルを合わせると、その瞬間の映像が出ます";
} else {
  hint.textContent = "";
}

function hms(s) {
  s = Math.max(0, Math.floor(s));
  const p = n => String(n).padStart(2, "0");
  return p(Math.floor(s / 3600)) + ":" + p(Math.floor(s % 3600 / 60)) + ":" + p(s % 60);
}

let W = 0, innerW = 0, innerH = 0, X = null, Y = null;

function draw() {
  W = Math.max(320, root.clientWidth);
  const H = D.plotH;
  innerW = W - PAD.l - PAD.r;
  innerH = H - PAD.t - PAD.b;
  X = t => PAD.l + (t / D.duration) * innerW;
  Y = v => PAD.t + innerH - (v / vmax) * innerH;

  // 目盛りの間隔は、6〜8本くらいになるものを選ぶ
  const steps = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200];
  const tick = steps.find(s => D.duration / s <= 8) || 7200;

  let g = "";
  for (let i = 1; i <= 3; i++) {
    const y = PAD.t + innerH * i / 4;
    g += `<line x1="${PAD.l}" y1="${y}" x2="${W - PAD.r}" y2="${y}"
           stroke="${C.grid}" stroke-width="1"/>`;
  }
  g += `<line x1="${PAD.l}" y1="${PAD.t + innerH}" x2="${W - PAD.r}" y2="${PAD.t + innerH}"
         stroke="${C.grid}" stroke-width="1"/>`;
  for (let t = 0; t <= D.duration; t += tick) {
    g += `<text class="lbl" x="${X(t)}" y="${H - 6}" text-anchor="middle">${hms(t)}</text>`;
  }

  let bands = "";
  D.clips.forEach(c => {
    bands += `<rect x="${X(c[0])}" y="${PAD.t}" width="${Math.max(1, X(c[1]) - X(c[0]))}"
              height="${innerH}" fill="${C.series}" opacity="${D.bandOpacity}"/>`;
  });

  let d = "";
  for (let i = 0; i < D.t.length; i++) {
    d += (i ? "L" : "M") + X(D.t[i]).toFixed(1) + " " + Y(D.v[i]).toFixed(1) + " ";
  }

  const ty = Y(D.threshold);
  plot.innerHTML = `<svg width="${W}" height="${H}">
    ${g}${bands}
    <path d="${d}" fill="none" stroke="${C.series}" stroke-width="2"
          stroke-linejoin="round" stroke-linecap="round"/>
    <line x1="${PAD.l}" y1="${ty}" x2="${W - PAD.r}" y2="${ty}"
          stroke="${C.ink}" stroke-width="1.5" stroke-dasharray="6 4"/>
    <text class="lbl" x="${PAD.l + 6}" y="${ty - 5}" fill="${C.ink}">
      しきい値 ${Math.round(D.threshold)}</text>
    <line id="cross" x1="0" y1="${PAD.t}" x2="0" y2="${PAD.t + innerH}"
          stroke="${C.muted}" stroke-width="1" opacity="0"/>
    <circle id="dot" r="4" fill="${C.series}" stroke="${C.surface}" stroke-width="2" opacity="0"/>
    <rect id="hit" x="${PAD.l}" y="${PAD.t}" width="${innerW}" height="${innerH}"
          fill="transparent" style="cursor:crosshair"/>
  </svg>`;

  const hit = document.getElementById("hit");
  hit.addEventListener("pointermove", onMove);
  hit.addEventListener("pointerleave", onLeave);
}

function onMove(e) {
  const r = plot.getBoundingClientRect();
  const x = e.clientX - r.left;
  const t = Math.min(D.duration, Math.max(0, (x - PAD.l) / innerW * D.duration));
  const i = Math.min(D.t.length - 1, Math.max(0, Math.round(t / step)));

  const cross = document.getElementById("cross");
  const dot = document.getElementById("dot");
  cross.setAttribute("x1", X(t)); cross.setAttribute("x2", X(t));
  cross.setAttribute("opacity", "0.55");
  dot.setAttribute("cx", X(D.t[i])); dot.setAttribute("cy", Y(D.v[i]));
  dot.setAttribute("opacity", "1");

  chip.textContent = hms(D.t[i]) + " ・ 音量 " + Math.round(D.v[i]);
  if (!D.sprite) { hint.textContent = chip.textContent; return; }

  const s = D.sprite;
  const idx = Math.min(s.count - 1, Math.floor(t / s.interval));
  const col = idx % s.cols, row = Math.floor(idx / s.cols);
  shot.style.backgroundPosition = `-${col * s.tile_w}px -${row * s.tile_h}px`;
  shot.style.left = Math.min(W - s.tile_w, Math.max(0, X(t) - s.tile_w / 2)) + "px";
  shot.style.display = "block";
  hint.style.display = "none";
}

function onLeave() {
  document.getElementById("cross").setAttribute("opacity", "0");
  document.getElementById("dot").setAttribute("opacity", "0");
  shot.style.display = "none";
  hint.style.display = "block";
  if (!D.sprite) hint.textContent = "";
}

draw();
new ResizeObserver(draw).observe(root);
</script>
"""


def volume_chart_html(rms, win_sec, threshold, clips, duration, sprite, c) -> tuple[str, int]:
    """音量の推移・しきい値・切り出し範囲を描き、ホバーでその瞬間のコマを見せる"""
    times, values = thin_out(rms, win_sec)
    data = {
        "t": times, "v": values, "threshold": threshold,
        "clips": [[round(s, 2), round(e, 2)] for s, e in clips],
        "duration": duration, "plotH": PLOT_H, "sprite": sprite,
        "bandOpacity": c["band_opacity"],
        "colors": {k: c[k] for k in ("series", "ink", "muted", "grid", "surface")},
    }
    height = (sprite["tile_h"] if sprite else 26) + PLOT_H + 8
    return CHART_TEMPLATE.replace("__DATA__", json.dumps(data)), height


st.set_page_config(page_title="spl-clip", page_icon="🎬", layout="wide")
st.title("🎬 盛り上がった瞬間を切り出す")

# ---------------- ① 動画を選ぶ ----------------
st.subheader("① 動画を選ぶ")
c_path, c_browse = st.columns([7, 1.4], vertical_alignment="bottom")

if c_browse.button("📁 参照", width="stretch"):
    picked = pick_file_dialog()
    if picked:
        st.session_state["video_path"] = picked
        st.rerun()
    else:
        st.toast("ファイルが選ばれませんでした")

path_str = c_path.text_input(
    "動画ファイルのパス",
    value=st.session_state.get("video_path", default_video()),
    placeholder="「📁 参照」で選ぶか、パスを貼り付けてください",
)

if not path_str.strip():
    st.info("「📁 参照」ボタンから動画を選んでください。")
    render_cache_panel()
    st.stop()

video = Path(path_str.strip().strip('"')).expanduser()
if not video.is_file():
    st.error(f"ファイルが見つかりません: {video}")
    render_cache_panel()
    st.stop()

# この動画に実際に入っている音声トラックだけを選べるようにする
tracks = load_tracks(str(video.resolve()), video.stat().st_mtime)
if tracks is not None and not tracks:
    st.error("この動画には音声が入っていません。別の動画を選んでください。")
    render_cache_panel()
    st.stop()

if tracks is None:
    track = st.number_input(
        "音声トラック", min_value=0, max_value=7, value=ac.CONFIG["audio_track"],
        help="トラックを自動で調べられませんでした。番号を直接指定してください",
    )
elif len(tracks) == 1:
    track = 0
    st.caption(
        f"この動画の音声は1本だけです({ac.describe_track(0, tracks[0])})。"
        "選ぶ必要はありません。"
    )
else:
    track = st.selectbox(
        f"どの音声で判定しますか?(この動画には {len(tracks)} 本あります)",
        options=range(len(tracks)),
        format_func=lambda i: ac.describe_track(i, tracks[i]),
        help="OBSなどで音を分けて録画した場合、マイクだけのトラックを選ぶと"
             "ゲーム音に埋もれずに自分の声で検出できます",
    )

wav = ensure_audio(video, int(track))
rms = load_rms(str(wav), wav.stat().st_mtime, WIN_SEC)
total_sec = len(rms) * WIN_SEC
st.caption(f"長さ {ac.sec_to_hms(total_sec)} / 音声トラック {int(track)} を解析対象にしています")

# ---------------- ② 切り出し方を決める ----------------
st.subheader("② 切り出し方を決める")
c1, c2, c3, c4 = st.columns(4)
percentile = c1.slider(
    "しきい値(上位%)", min_value=95.0, max_value=99.9, value=ac.CONFIG["percentile"], step=0.1,
    help="音量が上位何%なら盛り上がりとみなすか。数字を下げるほど候補が増えます",
)
before = c2.slider("何秒前から", 0, 180, int(ac.CONFIG["before_sec"]), 5)
after = c3.slider("何秒後まで", 0, 180, int(ac.CONFIG["after_sec"]), 5)
gap = c4.slider(
    "まとめる間隔(秒)", 0, 120, int(ac.CONFIG["merge_gap_sec"]), 5,
    help="盛り上がり同士がこの秒数以内なら、1本のクリップにまとめます",
)

threshold = float(np.percentile(rms, percentile))
times = ac.peak_times(rms, WIN_SEC, percentile)
clips = ac.merge_intervals(times, before, after, gap)

# ---------------- ③ 結果を見る ----------------
st.subheader(f"③ クリップ候補: {len(clips)} 件")
if not clips:
    st.info("候補がありません。しきい値のスライダーを左に動かして条件をゆるめてください。")
    render_cache_panel(video)
    st.stop()

total_clip_sec = sum(e - s for s, e in clips)
st.caption(
    f"検出された盛り上がり {len(times)} 箇所 → {len(clips)} 本にまとめました / "
    f"合計 {ac.sec_to_hms(total_clip_sec)}(元動画の {total_clip_sec / total_sec:.0%})"
)

st.markdown("**音量の推移**")
with st.spinner("プレビュー用のコマ画像を用意しています…(この動画では最初の1回だけ)"):
    sprite = load_sprite(str(video.resolve()), video.stat().st_mtime, total_sec)
chart_html, chart_h = volume_chart_html(
    rms, WIN_SEC, threshold, clips, total_sec, sprite, theme()
)
st.iframe(chart_html, height=chart_h)
st.caption(
    "青い線が音量、破線がしきい値です。塗りつぶした帯が実際に切り出される範囲を表します。"
    + ("カーソルを合わせると、その瞬間の映像がコマ送りで確認できます。" if sprite else "")
    + (f" (グラフは {MAX_POINTS} 点に間引いていますが、山の高さは保たれます)"
       if len(rms) > MAX_POINTS else "")
)

st.dataframe(
    [
        {
            "クリップ": f"clip_{i:03d}",
            "開始": ac.sec_to_hms(s),
            "終了": ac.sec_to_hms(e),
            "長さ(秒)": round(e - s),
        }
        for i, (s, e) in enumerate(clips, 1)
    ],
    width="stretch",
    hide_index=True,
)

# ---------------- ④ 書き出す ----------------
st.subheader("④ 書き出す")
if st.button(f"{len(clips)} 本のクリップを書き出す", type="primary"):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    bar = st.progress(0.0, text="準備中…")
    written = []
    for i, (s, e) in enumerate(clips, 1):
        out = OUT_DIR / f"clip_{i:03d}_{ac.sec_to_hms(s).replace(':', '')}.mp4"
        bar.progress((i - 1) / len(clips), text=f"{out.name} を書き出し中…({i}/{len(clips)})")
        ac.ffmpeg("-ss", f"{s:.1f}", "-i", video, "-t", f"{e - s:.1f}", "-c", "copy", out)
        written.append(str(out))
    bar.progress(1.0, text=f"完了! {len(written)} 本を書き出しました")
    st.session_state["written"] = written

if st.session_state.get("written"):
    written = st.session_state["written"]
    st.success(f"{OUT_DIR}/ に {len(written)} 本のクリップがあります")
    if st.button("📂 フォルダを開く"):
        opener = "explorer" if sys.platform.startswith("win") else "open"
        subprocess.run([opener, str(OUT_DIR.resolve())])

    st.subheader("⑤ 中身を確認する")
    st.caption(
        "ここで再生できない場合でもファイル自体は正常です"
        "(iPhone録画などブラウザが対応しない形式のことがあります)。"
        "その場合は「フォルダを開く」から QuickTime などで確認してください。"
    )
    for path in written:
        with st.expander(Path(path).name):
            st.video(path)

st.divider()
render_cache_panel(video)
