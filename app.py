#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
auto_clip.py のブラウザ操作版。

起動:
  streamlit run app.py
  (または 起動.command をダブルクリック)

検出ロジックは auto_clip.py をそのまま呼んでいるので、コマンド版と結果は同じになる。
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

import auto_clip as ac

VIDEO_EXTS = [".mov", ".mp4", ".mkv", ".avi", ".m4v"]
WIN_SEC = ac.CONFIG["win_sec"]
OUT_DIR = Path(ac.CONFIG["out_dir"])
CACHE_DIR = Path(ac.CONFIG["cache_dir"])
MAX_POINTS = 1500  # グラフに描く点の上限(これを超えたら間引く)

# 検証済みパレット(dataviz)。Streamlit の実際の背景色に対して検査済み
THEMES = {
    "light": {
        "series": "#2a78d6", "ink": "#52514e", "muted": "#898781",
        "grid": "#e1e0d9", "band_opacity": 0.10,
    },
    "dark": {
        "series": "#3987e5", "ink": "#c3c2b7", "muted": "#898781",
        "grid": "#2c2c2a", "band_opacity": 0.18,
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
    load_rms.clear()  # 解析結果のキャッシュも一緒に捨てる
    st.rerun()


def render_cache_panel(current: Path | None = None):
    """溜まった作業ファイルの量を見せて、その場で削除できるようにする"""
    files = sorted(CACHE_DIR.glob("*_audio.wav"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if CACHE_DIR.exists() else []
    total = sum(p.stat().st_size for p in files)

    with st.expander(f"🗂 作業ファイル(キャッシュ) — {len(files)} 個 / 合計 {fmt_size(total)}"):
        st.caption(
            "音量を調べるために動画から取り出した音声です。動画1時間あたり約110MBあり、"
            "自動では消えません。消しても次に同じ動画を開いたときに作り直されます"
            "(そのぶん待ち時間が増えるだけです)。"
        )
        if not files:
            st.info("作業ファイルはありません。")
            return

        st.dataframe(
            [
                {
                    "": "● 使用中" if current and p == current else "",
                    "元の動画": p.name.rsplit("_", 2)[0],
                    "サイズ": fmt_size(p.stat().st_size),
                    "最終使用": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                }
                for p in files
            ],
            width="stretch",
            hide_index=True,
        )

        others = [p for p in files if p != current]
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


def thin_out(rms: np.ndarray, win_sec: float) -> pd.DataFrame:
    """描画用に点を間引く。区間の最大値を残すので、山(ピーク)は消えない"""
    idx = np.arange(len(rms))
    if len(rms) > MAX_POINTS:
        k = int(np.ceil(len(rms) / MAX_POINTS))
        n = len(rms) // k
        block = rms[: n * k].reshape(n, k)
        idx = block.argmax(axis=1) + np.arange(n) * k
    return pd.DataFrame({
        "秒": idx * win_sec,
        "音量": rms[idx],
        "時刻": [ac.sec_to_hms(i * win_sec) for i in idx],
    })


def volume_chart(rms, win_sec, threshold, clips, c):
    """音量の推移・しきい値・切り出される範囲を1枚に重ねる"""
    df = thin_out(rms, win_sec)
    time_axis = alt.Axis(
        labelExpr="utcFormat(datum.value * 1000, '%H:%M:%S')", title=None,
        grid=True, gridColor=c["grid"], gridDash=[], domainColor=c["grid"],
        tickColor=c["grid"], labelColor=c["muted"], labelFontSize=11,
    )
    value_axis = alt.Axis(
        title=None, grid=True, gridColor=c["grid"], gridDash=[],
        domain=False, ticks=False, labelColor=c["muted"], labelFontSize=11,
    )

    # 切り出される範囲(背景の帯)
    spans = pd.DataFrame(
        [{"開始": s, "終了": e, "クリップ": f"clip_{i:03d}"} for i, (s, e) in enumerate(clips, 1)]
    )
    band = alt.Chart(spans).mark_rect(
        color=c["series"], opacity=c["band_opacity"]
    ).encode(
        x=alt.X("開始:Q", axis=time_axis, title=None),
        x2="終了:Q",
        tooltip=[alt.Tooltip("クリップ:N", title="切り出し範囲")],
    )

    line = alt.Chart(df).mark_line(color=c["series"], strokeWidth=2).encode(
        x=alt.X("秒:Q", axis=time_axis, title=None, scale=alt.Scale(nice=False)),
        y=alt.Y("音量:Q", axis=value_axis),
    )

    th_df = pd.DataFrame({"y": [threshold], "ラベル": [f"しきい値 {threshold:.0f}"]})
    th_rule = alt.Chart(th_df).mark_rule(
        color=c["ink"], strokeWidth=1.5, strokeDash=[6, 4]
    ).encode(y="y:Q")
    th_label = alt.Chart(th_df).mark_text(
        align="left", baseline="bottom", dx=6, dy=-4, fontSize=11, color=c["ink"]
    ).encode(y="y:Q", text="ラベル:N")

    # ホバーで時刻と音量を読めるようにする
    hover = alt.selection_point(nearest=True, on="pointermove", fields=["秒"], empty=False)
    crosshair = alt.Chart(df).mark_rule(color=c["muted"], strokeWidth=1).encode(
        x=alt.X("秒:Q", axis=time_axis, title=None),
        opacity=alt.condition(hover, alt.value(0.6), alt.value(0)),
        tooltip=[alt.Tooltip("時刻:N"), alt.Tooltip("音量:Q", format=".0f")],
    ).add_params(hover)

    return (band + line + th_rule + th_label + crosshair).properties(height=240)


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
    render_cache_panel(wav)
    st.stop()

total_clip_sec = sum(e - s for s, e in clips)
st.caption(
    f"検出された盛り上がり {len(times)} 箇所 → {len(clips)} 本にまとめました / "
    f"合計 {ac.sec_to_hms(total_clip_sec)}(元動画の {total_clip_sec / total_sec:.0%})"
)

st.markdown("**音量の推移**")
st.altair_chart(volume_chart(rms, WIN_SEC, threshold, clips, theme()), theme=None)
st.caption(
    "青い線が音量、破線がしきい値です。塗りつぶした帯が実際に切り出される範囲を表します。"
    "スライダーを動かすと帯の位置と数が変わります。"
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
render_cache_panel(wav)
