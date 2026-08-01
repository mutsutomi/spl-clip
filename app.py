#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_clip.py のブラウザ操作版。

起動:
  streamlit run app.py
  (または 起動.command をダブルクリック)

検出ロジックは auto_clip.py をそのまま呼んでいるので、コマンド版と結果は同じになる。
"""

from __future__ import annotations  # 古いPythonでも読み込めるようにする(エラーを分かりやすく)

import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np
import streamlit as st

import auto_clip as ac

VIDEO_EXTS = [".mov", ".mp4", ".mkv", ".avi", ".m4v"]
WIN_SEC = ac.CONFIG["win_sec"]
OUT_DIR = Path(ac.CONFIG["out_dir"])
CACHE_DIR = Path(ac.CONFIG["cache_dir"])
STATIC_DIR = Path("static")  # ブラウザに直接読ませるサムネイル画像の置き場所
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


def pick_folder_dialog() -> str | None:
    """OSの「フォルダを選ぶ」ダイアログを開いてパスを返す(キャンセル時は None)"""
    if sys.platform == "darwin":
        script = 'POSIX path of (choose folder with prompt "クリップの書き出し先フォルダを選んでください")'
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    elif sys.platform.startswith("win"):
        ps = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$d.Description = 'クリップの書き出し先フォルダを選んでください';"
            "if ($d.ShowDialog() -eq 'OK') { [Console]::Out.Write($d.SelectedPath) }"
        )
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps],
                           capture_output=True, text=True)
    else:
        return None
    return r.stdout.strip() or None


def default_video() -> str:
    """初期値として、このフォルダで一番新しい動画を選んでおく"""
    found = [p for p in Path(".").iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    if not found:
        return ""
    return str(max(found, key=lambda p: p.stat().st_mtime).resolve())


def ensure_audio(video: Path, track: int, force: bool = False) -> Path:
    """検出用WAVを用意する。トラックを変えたときだけ自動で取り込み直す。

    force は「使う音声を選び直した直後」だけ真になる。WAVの名前にトラック番号は
    入っていないため、既存ファイルが選んだトラックのものだと確認できないときに使う。
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    wav = ac.cache_wav_path(video, CACHE_DIR)
    key = (str(video.resolve()), track)

    # 既にあるWAVがどのトラックのものか分からない初回は、そのまま信用して使う
    stale = force or st.session_state.get("audio_key") not in (None, key)
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
def load_track_info(video_path: str, mtime: float, n_tracks: int):
    """各トラックの素性を調べ、試聴用の短い音声も用意する"""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    video = Path(video_path)
    duration = ac.probe_duration(video) or 0.0
    tag = hashlib.sha1(f"{video.resolve()}:{mtime}".encode()).hexdigest()[:8]

    info = []
    for i in range(n_tracks):
        sample = STATIC_DIR / f"samp_{tag}_a{i}.m4a"
        if not sample.exists():
            try:
                ac.extract_track_sample(video, sample, i, max(0.0, duration * 0.5 - 10))
            except (OSError, subprocess.CalledProcessError):
                pass
        info.append({
            "profile": ac.track_profile(video, i, duration),
            "sample": str(sample) if sample.exists() else None,
        })
    return info


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
    # メモリ上の解析結果・切り出し済み一時ファイルの記録も一緒に捨てる
    load_rms.clear()
    load_sprite.clear()
    load_track_info.clear()
    preview_candidate.clear()
    st.rerun()


def render_cache_panel(current_video: Path | None = None):
    """溜まった作業ファイルの量を見せて、その場で削除できるようにする"""
    kinds = {".wav": "音声", ".jpg": "コマ画像", ".m4a": "再生用の音声", ".mp4": "再生用の映像",
             ".mov": "再生用の映像", ".mkv": "再生用の映像"}
    files = sorted(
        (p for d in (CACHE_DIR, STATIC_DIR) if d.exists()
         for p in d.iterdir() if p.suffix in kinds),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    # ハードリンクはクリップと実体を共有しているので、容量は二重に数えない
    shared = {p for p in files if p.stat().st_nlink > 1}
    total = sum(p.stat().st_size for p in files if p not in shared)

    # 溜まりすぎたら、折りたたみを開かなくても気づけるように警告を出す
    if total > 1_000_000_000:
        st.warning(
            f"作業ファイルが合計 {fmt_size(total)} になっています。"
            "下の「🗂 作業ファイル」を開いて「すべて削除」すると空にできます"
            "(消しても、次に使うときに自動で作り直されます)。"
        )
    in_use = set()
    if current_video:
        in_use = {ac.cache_wav_path(current_video, CACHE_DIR)}
        sprite = st.session_state.get("sprite_in_use")
        if sprite:
            in_use.add(Path(sprite))

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
                    "種類": kinds[p.suffix],
                    "サイズ": "0 B(クリップと共有)" if p in shared else fmt_size(p.stat().st_size),
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
    """描画用に点を間引く。区間の最大値を残すので、山(ピーク)は消えない。

    時刻は必ず等間隔にする。画面側はカーソル位置から点を逆算するので、
    間隔がばらつくと読み取る時刻がずれてしまう。
    """
    if len(rms) <= MAX_POINTS:
        times, values = np.arange(len(rms)) * win_sec, rms
    else:
        k = int(np.ceil(len(rms) / MAX_POINTS))
        n = len(rms) // k
        values = rms[: n * k].reshape(n, k).max(axis=1)
        times = np.arange(n) * k * win_sec
    return times.round(2).tolist(), values.round(1).tolist()


@st.cache_data(show_spinner=False)
def load_sprite(video_path: str, mtime: float, duration: float):
    """プレビュー用サムネイルを用意する。作れなかった場合は None(グラフは出る)。

    画像は data URI で埋め込まず、Streamlit の静的配信(static/)経由で読ませる。
    長い動画では十数MBになるため、画面を更新するたびに送り直すと重すぎるため。
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    video = Path(video_path)
    out = ac.sprite_path(video, STATIC_DIR, ac.sprite_interval(duration))
    if not out.exists() and ac.make_sprite(video, out, duration) is None:
        return None
    size = ac.probe_size(out)
    if size is None:
        return None
    meta = ac.sprite_meta(duration, size)
    meta["url"] = f"app/static/{quote(out.name)}?v={int(out.stat().st_mtime)}"
    meta["path"] = str(out)
    return meta


@st.cache_data(show_spinner=False)
def preview_candidate(video_path: str, mtime: float, s: float, e: float, n_tracks: int):
    """候補の区間を一時ファイルとして切り出し、画面で再生できる形にする。

    書き出しとまったく同じコマンドで切るので、見えるものは書き出し結果と一致する。
    成果物ではなく static/ に置く作業ファイルなので、いつ消してもよい。
    """
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    video = Path(video_path)
    tag = hashlib.sha1(f"{video.resolve()}:{mtime}:{s:.1f}:{e:.1f}".encode()).hexdigest()[:8]

    clip = STATIC_DIR / f"prevc_{tag}.mp4"
    if not clip.exists():
        try:
            ac.ffmpeg("-ss", f"{s:.1f}", "-i", video, "-t", f"{e - s:.1f}",
                      *ac.CUT_MAP, "-c", "copy", clip)
        except (OSError, subprocess.CalledProcessError):
            return None

    extras = []
    for i in range(1, n_tracks):
        a = STATIC_DIR / f"prevc_{tag}_a{i}.m4a"
        if not a.exists():
            try:
                ac.extract_track_audio(clip, a, i)
            except (OSError, subprocess.CalledProcessError):
                continue
        if a.exists():
            extras.append(f"app/static/{quote(a.name)}")
    return {"video": f"app/static/{quote(clip.name)}?v={int(clip.stat().st_mtime)}",
            "extras": extras}


PLAYER_TEMPLATE = """
<meta charset="utf-8">
<style>
  body { margin: 0; background: transparent;
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  video { width: 100%; max-height: 420px; background: #000; border-radius: 6px; }
  #bar { display: flex; gap: 8px; justify-content: center; margin-top: 8px; }
  #bar button { font: 13px system-ui, -apple-system, "Segoe UI", sans-serif;
                padding: 6px 16px; border-radius: 6px; cursor: pointer;
                border: 1px solid rgba(128,128,128,.4); background: transparent; color: #444; }
  #bar button:hover { background: rgba(128,128,128,.15); }
  @media (prefers-color-scheme: dark) { #bar button { color: #ddd; } }
</style>
<video id="v" controls preload="metadata" src="__VIDEO__"></video>
<div id="bar">
  <button data-d="-30">« 30秒</button>
  <button data-d="-5">‹ 5秒</button>
  <button data-d="5">5秒 ›</button>
  <button data-d="30">30秒 »</button>
</div>
<div id="extras"></div>
<script>
const EXTRAS = __EXTRAS__;
const v = document.getElementById("v");
const box = document.getElementById("extras");

// 追加の音声(マイクなど)。動画に合わせて鳴らす
const tracks = EXTRAS.map(src => {
  const a = document.createElement("audio");
  a.src = src; a.preload = "metadata"; a.style.display = "none";
  box.appendChild(a);
  return a;
});

const each = fn => tracks.forEach(fn);
const align = () => each(a => { if (Math.abs(a.currentTime - v.currentTime) > 0.2) a.currentTime = v.currentTime; });

v.addEventListener("play",  () => { align(); each(a => a.play().catch(() => {})); });
v.addEventListener("pause", () => each(a => a.pause()));
v.addEventListener("ended", () => each(a => a.pause()));
v.addEventListener("seeking", () => each(a => { a.currentTime = v.currentTime; }));
v.addEventListener("waiting", () => each(a => a.pause()));
v.addEventListener("playing", () => { align(); each(a => a.play().catch(() => {})); });
v.addEventListener("ratechange", () => each(a => { a.playbackRate = v.playbackRate; }));
v.addEventListener("volumechange", () => each(a => { a.volume = v.volume; a.muted = v.muted; }));
v.addEventListener("timeupdate", align);   // ずれてきたら合わせ直す

// 前後送りボタン(追加音声は seeking イベント経由で追従する)
document.querySelectorAll("#bar button").forEach(b => b.onclick = () => {
  const d = parseFloat(b.dataset.d);
  v.currentTime = Math.max(0, Math.min(v.duration || 1e9, v.currentTime + d));
});
</script>
"""


def player_html(src: dict) -> str:
    return (PLAYER_TEMPLATE
            .replace("__VIDEO__", src["video"])
            .replace("__EXTRAS__", json.dumps(src["extras"])))


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
const SC = D.spriteScale || 1;   // サムネイルの表示倍率(元画像をCSSで拡大する)
const bandH = D.sprite ? Math.round(D.sprite.tile_h * SC) : 0;
band.style.height = (D.sprite ? bandH : 26) + "px";

if (D.sprite) {
  shot.style.width = Math.round(D.sprite.tile_w * SC) + "px";
  shot.style.height = Math.round(D.sprite.tile_h * SC) + "px";
  shot.style.backgroundImage = "url(" + D.sprite.url + ")";
  shot.style.backgroundSize = Math.round(D.sprite.cols * D.sprite.tile_w * SC) + "px auto";
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
  const tw = Math.round(s.tile_w * SC), th = Math.round(s.tile_h * SC);
  shot.style.backgroundPosition = `-${col * tw}px -${row * th}px`;
  shot.style.left = Math.min(W - tw, Math.max(0, X(t) - tw / 2)) + "px";
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


def volume_chart_html(rms, win_sec, threshold, clips, duration, sprite, c,
                      sprite_scale: float = 1.0) -> tuple[str, int]:
    """音量の推移・しきい値・切り出し範囲を描き、ホバーでその瞬間のコマを見せる"""
    times, values = thin_out(rms, win_sec)
    data = {
        "t": times, "v": values, "threshold": threshold,
        "clips": [[round(s, 2), round(e, 2)] for s, e in clips],
        "duration": duration, "plotH": PLOT_H, "sprite": sprite,
        "spriteScale": sprite_scale,
        "bandOpacity": c["band_opacity"],
        "colors": {k: c[k] for k in ("series", "ink", "muted", "grid", "surface")},
    }
    height = (round(sprite["tile_h"] * sprite_scale) if sprite else 26) + PLOT_H + 8
    return CHART_TEMPLATE.replace("__DATA__", json.dumps(data)), height


st.set_page_config(page_title="spl-clip", page_icon="🎬", layout="wide")
st.title("🎬 盛り上がった瞬間を切り出す")

# ---------------- ① 動画を選ぶ ----------------
st.subheader("① 動画を選ぶ")
c_path, c_browse = st.columns([7, 1.4], vertical_alignment="bottom")

if c_browse.button("📁 参照", key="browse_video", width="stretch"):
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
    # 音声が複数あるときは、どれを使うか決まるまで取り込みを始めない
    # (先に取り込むと、選び直したときに無駄な待ち時間が発生するため)
    with st.spinner("それぞれの音声がどんな音か調べています…"):
        info = load_track_info(str(video.resolve()), video.stat().st_mtime, len(tracks))

    choice = st.radio(
        f"どの音声で判定しますか?(この動画には {len(tracks)} 本あります)",
        options=range(len(tracks)),
        format_func=lambda i: ac.describe_track(i, tracks[i], info[i]["profile"]),
        help="OBSなどで音を分けて録画した場合、マイクだけのトラックを選ぶと"
             "ゲーム音に埋もれずに自分の声で検出できます",
    )
    st.caption("迷ったら聞いてみてください(動画の中ほどから15秒)")
    for i in range(len(tracks)):
        c1, c2 = st.columns([1, 6], vertical_alignment="center")
        c1.markdown(f"トラック {i}")
        if info[i]["sample"]:
            c2.audio(info[i]["sample"])
        else:
            c2.caption("試聴を用意できませんでした")
    decided = (str(video.resolve()), int(choice))
    if st.session_state.get("track_decided") != decided:
        if st.button("この音声で解析する", type="primary"):
            st.session_state["track_decided"] = decided
            st.session_state["force_extract"] = True
            st.rerun()
        st.info(
            "使う音声を選んでから「この音声で解析する」を押してください。"
            "取り込みには少し時間がかかるので、選び終わってから始めます。"
        )
        render_cache_panel(video)
        st.stop()
    track = choice

wav = ensure_audio(video, int(track), force=st.session_state.pop("force_extract", False))
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

c_ttl, c_zoom = st.columns([5, 2], vertical_alignment="bottom")
c_ttl.markdown("**音量の推移**")
zoom = c_zoom.selectbox(
    "サムネイルの大きさ", ["標準", "大", "特大"], index=1,
    help="カーソルを合わせたときに出るコマ映像の表示サイズ(標準240 / 大360 / 特大480ピクセル)",
)
with st.spinner("プレビュー用のコマ画像を用意しています…(この動画では最初の1回だけ)"):
    sprite = load_sprite(str(video.resolve()), video.stat().st_mtime, total_sec)
st.session_state["sprite_in_use"] = sprite["path"] if sprite else None
chart_html, chart_h = volume_chart_html(
    rms, WIN_SEC, threshold, clips, total_sec, sprite, theme(),
    sprite_scale={"標準": 1.0, "大": 1.5, "特大": 2.0}[zoom],
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

# ---------------- ④ 中身を確認する ----------------
st.subheader("④ 中身を確認する")
st.caption(
    "書き出す前に、候補をその場で再生して確認できます。"
    "ここで見えるものは、書き出されるクリップとまったく同じです(確認用は一時ファイルで、保存はされません)。"
)
if st.session_state.get("pick_clip", 0) >= len(clips):
    st.session_state["pick_clip"] = 0

def step_clip(d: int):
    st.session_state["pick_clip"] = (st.session_state.get("pick_clip", 0) + d) % len(clips)

c_prev, c_pick, c_next = st.columns([1.2, 5, 1.2], vertical_alignment="bottom")
c_prev.button("◀ 前", key="clip_prev", width="stretch",
              disabled=len(clips) < 2, on_click=step_clip, args=(-1,))
c_next.button("次 ▶", key="clip_next", width="stretch",
              disabled=len(clips) < 2, on_click=step_clip, args=(1,))
pick = c_pick.selectbox(
    "確認するクリップ候補", options=range(len(clips)),
    format_func=lambda k: (
        f"clip_{k + 1:03d}: {ac.sec_to_hms(clips[k][0])} 〜 {ac.sec_to_hms(clips[k][1])}"
        f" ({clips[k][1] - clips[k][0]:.0f}秒)"
    ),
    key="pick_clip",
)
n_tracks = len(tracks) if tracks else 1
with st.spinner("確認用に切り出しています…"):
    src = preview_candidate(str(video.resolve()), video.stat().st_mtime,
                            clips[pick][0], clips[pick][1], n_tracks)
if src:
    st.iframe(player_html(src), height=505)
    if src["extras"]:
        st.caption(
            f"この動画は音声が {n_tracks} 本あります。ブラウザは1本しか鳴らせないため、"
            "残りを重ねて同時に再生しています(音量は変えていません)。"
        )
    st.caption(
        "映像が出ない場合でもクリップは正常です"
        "(iPhone録画などブラウザが対応しない形式のことがあります)。"
        "その場合は書き出したあとに QuickTime などで確認してください。"
    )
else:
    st.warning("確認用の切り出しに失敗しました。書き出し自体は試せます。")

# ---------------- ⑤ 書き出す ----------------
st.subheader("⑤ 書き出す")
c_out, c_ob = st.columns([6, 1.4], vertical_alignment="bottom")
if c_ob.button("📁 参照", key="browse_out", width="stretch"):
    picked_dir = pick_folder_dialog()
    if picked_dir:
        st.session_state["out_dir"] = picked_dir
        st.rerun()
    else:
        st.toast("フォルダが選ばれませんでした")
out_str = c_out.text_input(
    "書き出し先フォルダ",
    value=st.session_state.get("out_dir", str(OUT_DIR.resolve())),
    help="クリップ動画の保存先です。再生確認用の作業ファイルはここには作られません",
)
out_dir = Path(out_str.strip().strip('"')).expanduser()

if st.button(f"{len(clips)} 本のクリップを書き出す", type="primary"):
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        st.error(f"書き出し先フォルダを作れませんでした: {e}")
        st.stop()
    bar = st.progress(0.0, text="準備中…")
    written = []
    for i, (s, e) in enumerate(clips, 1):
        out = out_dir / f"clip_{i:03d}_{ac.sec_to_hms(s).replace(':', '')}.mp4"
        bar.progress((i - 1) / len(clips), text=f"{out.name} を書き出し中…({i}/{len(clips)})")
        ac.ffmpeg("-ss", f"{s:.1f}", "-i", video, "-t", f"{e - s:.1f}",
                  *ac.CUT_MAP, "-c", "copy", out)
        written.append(str(out))
    bar.progress(1.0, text=f"完了! {len(written)} 本を書き出しました")
    st.session_state["written"] = written

written = [p for p in st.session_state.get("written", []) if Path(p).is_file()]
if written:
    written_dir = Path(written[0]).parent
    st.success(f"{written_dir}/ に {len(written)} 本のクリップがあります")
    if st.button("📂 フォルダを開く"):
        opener = "explorer" if sys.platform.startswith("win") else "open"
        subprocess.run([opener, str(written_dir.resolve())])

st.divider()
render_cache_panel(video)
