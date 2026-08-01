#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
動画の「音が大きい瞬間」を検出し、その前後を自動で切り出すツール。

使い方:
  python3 auto_clip.py 動画ファイル.mov                 # 検出して切り出しまで実行
  python3 auto_clip.py 動画ファイル.mov --dry-run       # 切り出さず、候補の確認だけ
  python3 auto_clip.py 動画ファイル.mov --percentile 99 # 候補を増やしたいとき
  python3 auto_clip.py 動画ファイル.mov --track 1       # OBSマルチトラック録画でマイク音声を使うとき

依存: ffmpeg, numpy
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

# ============ 設定(ここを書き換えてもいいし、コマンドライン引数で上書きも可) ============
CONFIG = {
    "percentile": 99.5,    # 音量が上位何%なら「盛り上がり」とみなすか。小さくすると候補が増える(例: 99.0)
    "win_sec": 0.5,        # 音量を測る区切りの長さ(秒)。基本は触らなくてよい
    "before_sec": 60.0,    # 検出点の何秒前から切り出すか
    "after_sec": 60.0,     # 検出点の何秒後まで切り出すか
    "merge_gap_sec": 10.0, # ピーク同士がこの秒数以内なら1本のクリップにまとめる
    "audio_track": 0,      # 使う音声トラック番号(0始まり)。OBSでマイクが別トラックなら 1 などに
    "out_dir": "clips",    # クリップ(成果物)の出力先フォルダ
    "cache_dir": "cache",  # 検出用WAVなど作業ファイルの置き場所
}
# ====================================================================================


def sec_to_hms(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def ffmpeg(*args):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    subprocess.run(cmd, check=True)


def audio_tracks(video: Path) -> list[dict] | None:
    """動画に入っている音声トラックの一覧。調べられなかった場合は None"""
    cmd = [
        "ffprobe", "-hide_banner", "-loglevel", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_name,channels:stream_tags=title",
        "-of", "json", str(video),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return json.loads(out).get("streams", [])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def describe_track(i: int, stream: dict) -> str:
    """「トラック 1: モノラル(マイク)」のような説明文をつくる"""
    ch = {1: "モノラル", 2: "ステレオ"}.get(stream.get("channels"), f"{stream.get('channels', '?')}ch")
    title = (stream.get("tags") or {}).get("title")
    name = f"トラック {i}: {ch}"
    return f"{name}({title})" if title else name


def cache_wav_path(video: Path, cache_dir: Path) -> Path:
    """動画ごとに一意な検出用WAVのパス。別フォルダにある同名の動画とぶつからないようにする"""
    tag = hashlib.sha1(str(video.resolve()).encode()).hexdigest()[:8]
    return cache_dir / f"{video.stem}_{tag}_audio.wav"


def extract_audio(video: Path, wav: Path, track: int, refresh: bool):
    """検出用のWAVを抽出する。前回のものがあれば再利用(パラメータ調整の再実行が速くなる)"""
    if wav.exists() and not refresh:
        print(f"[audio] 抽出済みの音声を再利用: {wav}(作り直すには --refresh-audio)")
        return
    print(f"[audio] 音声を抽出中... ({video.name} → {wav})")
    ffmpeg("-i", video, "-map", f"0:a:{track}", "-vn", "-ac", 1, "-ar", 16000, wav)


def compute_rms(wav: Path, win_sec: float) -> np.ndarray:
    """WAVをwin_sec秒ごとに区切り、各区間のRMS音量を返す(重いのでUI側ではキャッシュする)"""
    with wave.open(str(wav), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)

    win = int(sr * win_sec)
    n_win = len(data) // win
    return np.sqrt((data[: n_win * win].reshape(n_win, win) ** 2).mean(axis=1))


def peak_times(rms: np.ndarray, win_sec: float, percentile: float) -> list[float]:
    """RMS音量が上位percentile%を超えた時刻(秒)のリストを返す"""
    th = np.percentile(rms, percentile)
    return [i * win_sec for i in range(len(rms)) if rms[i] >= th]


def detect_peaks(wav: Path, win_sec: float, percentile: float) -> list[float]:
    """WAVを解析し、音量ピークの時刻(秒)のリストを返す"""
    rms = compute_rms(wav, win_sec)
    times = peak_times(rms, win_sec, percentile)
    print(f"[detect] 動画の長さ: {sec_to_hms(len(rms) * win_sec)} / "
          f"閾値(上位{100 - percentile:g}%): {np.percentile(rms, percentile):.0f} / "
          f"ヒット: {len(times)}箇所")
    return times


def merge_intervals(times, before, after, gap):
    """ピーク時刻同士がgap秒以内なら1グループに統合し、各グループの前後にbefore/afterを付けて区間化する"""
    groups = []
    for t in sorted(times):
        if groups and t - groups[-1][1] <= gap:
            groups[-1][1] = t
        else:
            groups.append([t, t])
    return [[max(0.0, s - before), e + after] for s, e in groups]


def main():
    ap = argparse.ArgumentParser(
        description="音が大きい瞬間の前後を自動で切り出す",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("video", type=Path, help="対象の動画ファイル(.mov / .mp4 / .mkv など)")
    ap.add_argument("--percentile", type=float, default=CONFIG["percentile"], help="音量ピークの閾値(百分位)")
    ap.add_argument("--before", type=float, default=CONFIG["before_sec"], help="検出点の何秒前から切るか")
    ap.add_argument("--after", type=float, default=CONFIG["after_sec"], help="検出点の何秒後まで切るか")
    ap.add_argument("--gap", type=float, default=CONFIG["merge_gap_sec"], help="ピーク統合の間隔(秒)")
    ap.add_argument("--track", type=int, default=CONFIG["audio_track"], help="音声トラック番号(0始まり)")
    ap.add_argument("--out-dir", type=Path, default=Path(CONFIG["out_dir"]), help="クリップの出力先フォルダ")
    ap.add_argument("--cache-dir", type=Path, default=Path(CONFIG["cache_dir"]), help="作業ファイル(抽出WAV)の置き場所")
    ap.add_argument("--dry-run", action="store_true", help="切り出しは行わず、候補の一覧表示のみ")
    ap.add_argument("--refresh-audio", action="store_true", help="抽出済みWAVを使わず作り直す")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg が見つかりません。brew install ffmpeg でインストールしてください")
    if not args.video.exists():
        sys.exit(f"動画ファイルが見つかりません: {args.video}")

    tracks = audio_tracks(args.video)
    if tracks is not None:
        if not tracks:
            sys.exit(f"この動画には音声が入っていません: {args.video.name}")
        if args.track >= len(tracks):
            avail = "\n".join("  " + describe_track(i, s) for i, s in enumerate(tracks))
            sys.exit(f"音声トラック {args.track} はありません。この動画にあるのは:\n{avail}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    # 1) 音声抽出(キャッシュあり)
    wav = cache_wav_path(args.video, args.cache_dir)
    extract_audio(args.video, wav, args.track, args.refresh_audio)

    # 2) 音量ピーク検出
    times = detect_peaks(wav, CONFIG["win_sec"], args.percentile)
    if not times:
        sys.exit("ピークが検出されませんでした。--percentile を下げて(例: 99)再実行してみてください")

    # 3) 区間の統合
    clips = merge_intervals(times, args.before, args.after, args.gap)

    print(f"\n=== クリップ候補: {len(clips)} 件 ===")
    for i, (s, e) in enumerate(clips, 1):
        print(f"  clip_{i:03d}: {sec_to_hms(s)} 〜 {sec_to_hms(e)} ({e - s:.0f}秒)")

    if args.dry_run:
        print("\n--dry-run のため切り出しはスキップしました。"
              "候補数を調整したい場合は --percentile を変えて再実行してください")
        return

    # 4) 切り出し
    print()
    for i, (s, e) in enumerate(clips, 1):
        out = args.out_dir / f"clip_{i:03d}_{sec_to_hms(s).replace(':', '')}.mp4"
        print(f"[cut] {out.name} を書き出し中...")
        ffmpeg("-ss", f"{s:.1f}", "-i", args.video, "-t", f"{e - s:.1f}", "-c", "copy", out)

    print(f"\n完了! {len(clips)} 本のクリップを {args.out_dir}/ に書き出しました")


if __name__ == "__main__":
    main()
