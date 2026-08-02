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

from __future__ import annotations  # 古いPythonでも読み込めるようにする(エラーを分かりやすく)

import argparse
import hashlib
import json
import math
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

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


BIN_DIR = Path(__file__).resolve().parent / "bin"


def tool(name: str) -> str:
    """準備スクリプトが bin/ に置いた実行ファイルを優先し、無ければPATH上のものを使う"""
    exe = BIN_DIR / (f"{name}.exe" if sys.platform.startswith("win") else name)
    return str(exe) if exe.exists() else name


# 切り出し時に含めるストリーム。音声を明示しないと ffmpeg が「一番良い1本」だけを選び、
# OBSのマルチトラック録画ではマイクの音声が捨てられてしまう。
CUT_MAP = ("-map", "0:v:0", "-map", "0:a")


def sec_to_hms(t: float) -> str:
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def ffmpeg(*args):
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", *map(str, args)]
    subprocess.run(cmd, check=True)


def audio_tracks(video: Path) -> list[dict] | None:
    """動画に入っている音声トラックの一覧。調べられなかった場合は None"""
    cmd = [
        tool("ffprobe"), "-hide_banner", "-loglevel", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_name,channels:stream_tags=title",
        "-of", "json", str(video),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        return json.loads(out).get("streams", [])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def probe_duration(video: Path) -> float | None:
    """動画の長さ(秒)"""
    cmd = [tool("ffprobe"), "-hide_banner", "-loglevel", "error",
           "-show_entries", "format=duration", "-of", "json", str(video)]
    try:
        d = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        return float(d["format"]["duration"])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError, ValueError):
        return None


def track_pcm(video: Path, track: int, at_sec: float, dur: float, sr: int = 16000):
    """指定トラックの一部を2chのPCMとして読み込む(中間ファイルを作らない)"""
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", str(at_sec),
           "-i", str(video), "-map", f"0:a:{track}", "-t", str(dur), "-vn",
           "-ac", "2", "-ar", str(sr), "-f", "s16le", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    if len(raw) < 4000:
        return None
    return np.frombuffer(raw[: len(raw) // 4 * 4], dtype=np.int16).astype(np.float32).reshape(-1, 2)


def track_profile(video: Path, track: int, duration: float, dur: float = 15.0) -> dict | None:
    """音声トラックの素性を測る。

    左右が完全に同じ波形かどうか(1本の音源か)が一番あてになる手がかりで、
    喋っていない場面でも変わらない。無音の割合は場面によって振れるので、
    動画の3箇所を測って平均する。
    """
    spots = [duration * f for f in (0.25, 0.5, 0.75)]
    lrs, quiets, levels = [], [], []
    for at in spots:
        d = track_pcm(video, track, max(0.0, at - dur / 2), dur)
        if d is None:
            continue
        L, R = d[:, 0], d[:, 1]
        mono = L + R
        if np.abs(mono).max() < 1:
            quiets.append(1.0)
            continue
        win = 1600
        n = max(1, len(mono) // win)
        rms = np.sqrt((mono[: n * win].reshape(n, win) ** 2).mean(axis=1))
        lr = 1.0 if np.array_equal(L, R) else float(np.corrcoef(L, R)[0, 1])
        lrs.append(0.0 if np.isnan(lr) else lr)
        quiets.append(float((rms < max(rms.max() * 0.05, 1.0)).mean()))
        levels.append(float(20 * np.log10(max(np.sqrt((mono ** 2).mean()), 1.0) / 32768)))

    if not quiets:
        return None
    if not lrs:
        return {"silent": True}
    return {
        "silent": False,
        "lr": min(lrs),                      # 1箇所でも広がりがあればステレオ音源
        "quiet": sum(quiets) / len(quiets),
        "dbfs": max(levels),
    }


def extract_track_sample(video: Path, out: Path, track: int, at_sec: float, dur: float = 15.0):
    """試聴用の短い音声。ブラウザで鳴らすのでAACに変換する(15秒なので一瞬)"""
    ffmpeg("-ss", at_sec, "-i", video, "-map", f"0:a:{track}", "-t", dur, "-vn",
           "-c:a", "aac", "-b:a", 128_000, out)


def describe_track(i: int, stream: dict, profile: dict | None = None) -> str:
    """「トラック 1: モノラル音源・無音が多い → マイクの可能性」のような説明文をつくる"""
    ch = {1: "モノラル", 2: "ステレオ"}.get(stream.get("channels"), f"{stream.get('channels', '?')}ch")
    parts = [ch]
    title = (stream.get("tags") or {}).get("title")
    if title:
        parts.append(title)

    guess = None
    if profile and not profile.get("silent"):
        mono_src = profile["lr"] > 0.999
        parts.append("左右が同じ(音源1本)" if mono_src else "左右に広がりあり")
        if profile["quiet"] > 0.35:
            parts.append("音が途切れる")
            if mono_src:
                guess = "マイクの可能性"
        elif profile["quiet"] < 0.10:
            parts.append("鳴りっぱなし")
    elif profile and profile.get("silent"):
        parts.append("ほぼ無音")

    name = f"トラック {i}: " + "・".join(parts)
    return f"{name} → {guess}" if guess else name


def cache_path(video: Path, cache_dir: Path, kind: str, ext: str) -> Path:
    """動画ごとに一意な作業ファイルのパス。別フォルダにある同名の動画とぶつからないようにする"""
    tag = hashlib.sha1(str(video.resolve()).encode()).hexdigest()[:8]
    return cache_dir / f"{video.stem}_{tag}_{kind}.{ext}"


def cache_wav_path(video: Path, cache_dir: Path) -> Path:
    """検出用WAVのパス"""
    return cache_path(video, cache_dir, "audio", "wav")


def sprite_path(video: Path, cache_dir: Path, interval: float) -> Path:
    """プレビュー用サムネイル(コマ画像を1枚にまとめたもの)のパス。

    間隔と解像度を名前に含めるので、設定が変わったときは自動的に別ファイルとして作り直される。
    """
    return cache_path(video, cache_dir, f"sprite{int(interval)}s{SPRITE_WIDTH}w", "jpg")


def probe_size(image: Path) -> tuple[int, int] | None:
    """画像の縦横サイズ"""
    cmd = [tool("ffprobe"), "-hide_banner", "-loglevel", "error", "-select_streams", "v",
           "-show_entries", "stream=width,height", "-of", "json", str(image)]
    try:
        s = json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True).stdout)
        st = s["streams"][0]
        return int(st["width"]), int(st["height"])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError, KeyError, IndexError):
        return None


SPRITE_INTERVAL = 5.0    # 何秒ごとにコマを取るか(短めの動画のとき)
SPRITE_WIDTH = 240       # コマ1枚の横幅(px)。表示で拡大してもぼやけない程度に大きめ
SPRITE_COLS = 12         # 横に並べる枚数
SPRITE_MAX_TILES = 1000  # コマ数の上限。長い動画で画像が巨大になりブラウザが描けなくなるのを防ぐ


def sprite_interval(duration: float) -> float:
    """コマの間隔。長い動画では間隔を広げて、画像が大きくなりすぎないようにする"""
    return float(max(SPRITE_INTERVAL, math.ceil(duration / SPRITE_MAX_TILES)))


def sprite_meta(duration: float, size: tuple[int, int]) -> dict:
    """サムネイル画像のタイル構成。生成時と読み込み時で同じ計算をする"""
    interval = sprite_interval(duration)
    count = max(1, math.ceil(duration / interval))
    rows = math.ceil(count / SPRITE_COLS)
    return {
        "cols": SPRITE_COLS, "rows": rows, "count": count, "interval": interval,
        "tile_w": size[0] // SPRITE_COLS, "tile_h": size[1] // rows,
    }


def make_sprite(video: Path, out: Path, duration: float) -> dict | None:
    """一定間隔のコマを1枚のJPEGに並べる(YouTubeのシークバー画像と同じ仕組み)。

    キーフレームだけを読む(-skip_frame nokey)ので、全部を読む場合の1/3ほどの時間で済む。
    """
    meta = sprite_meta(duration, (0, 0))
    try:
        ffmpeg("-skip_frame", "nokey", "-i", video,
               "-vf", f"fps=1/{meta['interval']},scale={SPRITE_WIDTH}:-1,"
                      f"tile={SPRITE_COLS}x{meta['rows']}",
               "-frames:v", 1, "-q:v", 7, out)
    except (OSError, subprocess.CalledProcessError):
        return None

    size = probe_size(out)
    return sprite_meta(duration, size) if size else None


def mix_preview(clip: Path, out: Path, n_tracks: int):
    """再生確認用に、音声を1本へ重ねた動画を作る。

    ブラウザは動画に複数の音声が入っていても1本しか鳴らせないため、
    アプリの画面で「ゲーム音とマイクの両方」を聞くにはこれが要る。
    normalize=0 なので各音声の大きさは元のまま足し合わされ、音量バランスは変わらない。
    映像は無劣化のままコピーするので、書き出しは短時間で済む。
    """
    srcs = "".join(f"[0:a:{i}]" for i in range(n_tracks))
    ffmpeg("-i", clip,
           "-filter_complex", f"{srcs}amix=inputs={n_tracks}:normalize=0[a]",
           "-map", "0:v:0", "-map", "[a]",
           "-c:v", "copy", "-c:a", "aac", "-b:a", 192_000, out)


# ============ キル表示(「をたおした!」)の検出 ============
# スプラトゥーン3では、敵をたおすと画面下端の決まった位置に「◯◯をたおした!」が出る。
# その帯を1秒ごとに切り出し、同梱のお手本画像と照合して一致した時刻を「キルの瞬間」とする。
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
KILL_TEMPLATE = ASSETS_DIR / "taoshita_1080p.png"
KILL_BAND = (600, 960, 720, 96)   # 1080p基準の帯: x, y, 幅, 高さ(他解像度は高さ比で換算)
KILL_SCAN_W, KILL_SCAN_H = 240, 32   # 照合は縮小して行う(精度は実測で確認済み)
KILL_TMPL_W, KILL_TMPL_H = 60, 14
KILL_THRESHOLD = 0.8   # 一致度のしきい値。表示中は0.99前後、非表示時は0.6以下(実測)


def load_kill_template(path: Path | None = None):
    """お手本画像を照合用の縮小グレースケールとして読み込む"""
    p = Path(path) if path else KILL_TEMPLATE
    if not p.is_file():
        return None
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(p),
           "-vf", f"scale={KILL_TMPL_W}:{KILL_TMPL_H},format=gray", "-f", "rawvideo", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    if len(raw) < KILL_TMPL_W * KILL_TMPL_H:
        return None
    t = np.frombuffer(raw[: KILL_TMPL_W * KILL_TMPL_H], dtype=np.uint8) \
        .astype(np.float32).reshape(KILL_TMPL_H, KILL_TMPL_W)
    return (t - t.mean()) / (t.std() + 1e-6)


def _kill_band_frames(video: Path, start: float, dur: float, size: tuple[int, int]):
    """キル表示が出る帯だけを1秒ごとに切り出す(小さいのでデコード以外のコストはほぼゼロ)"""
    w, h = size
    s = h / 1080.0
    x, y, bw, bh = (round(v * s) for v in KILL_BAND)
    x = max(0, min(x, w - bw))
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", str(start),
           "-i", str(video), "-t", str(dur),
           "-vf", f"fps=1,crop={bw}:{bh}:{x}:{y},scale={KILL_SCAN_W}:{KILL_SCAN_H},format=gray",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (KILL_SCAN_W * KILL_SCAN_H)
    return np.frombuffer(raw[: n * KILL_SCAN_W * KILL_SCAN_H], dtype=np.uint8) \
        .reshape(n, KILL_SCAN_H, KILL_SCAN_W).astype(np.float32)


def _match_score(frame, tmpl) -> float:
    """帯の中でお手本と一番似ている場所の一致度(-1〜1)。名前の長さで表示位置がずれても拾える"""
    w = sliding_window_view(frame, (KILL_TMPL_H, KILL_TMPL_W))
    mu = w.mean(axis=(2, 3), keepdims=True)
    sd = w.std(axis=(2, 3), keepdims=True) + 1e-6
    ncc = np.einsum("ijkl,kl->ij", (w - mu) / sd, tmpl) / tmpl.size
    return float(ncc.max())


def scan_kills(video: Path, duration: float, template=None,
               threshold: float = KILL_THRESHOLD, chunk: float = 300.0,
               on_progress=None) -> list[float] | None:
    """キル表示が現れた時刻(秒)のリストを返す。走査できない場合は None。

    表示は数秒続くので、連続して一致している間は最初の1秒だけを記録する。
    """
    tmpl = template if isinstance(template, np.ndarray) else load_kill_template(template)
    size = probe_size(video)
    if tmpl is None or size is None:
        return None

    events: list[float] = []
    prev_hit = False
    t0 = 0.0
    while t0 < duration:
        d = min(chunk, duration - t0)
        for i, f in enumerate(_kill_band_frames(video, t0, d, size)):
            hit = _match_score(f, tmpl) >= threshold
            if hit and not prev_hit:
                events.append(t0 + i)
            prev_hit = hit
        t0 += chunk
        if on_progress:
            on_progress(min(1.0, t0 / duration))
    return events


def kills_cache_path(video: Path, cache_dir: Path) -> Path:
    """キル走査の結果キャッシュ(JSON)のパス"""
    return cache_path(video, cache_dir, "kills", "json")


def scan_kills_cached(video: Path, cache_dir: Path, duration: float,
                      on_progress=None, refresh: bool = False) -> list[float] | None:
    """キル走査の結果をキャッシュ付きで返す。走査は動画1時間あたり3〜4分かかるため"""
    p = kills_cache_path(video, cache_dir)
    if p.exists() and not refresh:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d.get("times"), list):
                return [float(t) for t in d["times"]]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass  # 壊れたキャッシュは作り直す
    times = scan_kills(video, duration, on_progress=on_progress)
    if times is None:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"times": times}), encoding="utf-8")
    return times


def extract_track_audio(clip: Path, out: Path, track: int):
    """指定した音声トラックだけを、再エンコードせずに取り出す。

    ブラウザは動画に入っている2本目以降の音声を鳴らせないため、画面で全部の音を
    聞くには、追加ぶんを別ファイルにして動画と同時に再生させる必要がある。
    音を作り変えないので、音量バランスは元のまま。
    """
    ffmpeg("-i", clip, "-map", f"0:a:{track}", "-vn", "-c:a", "copy", out)


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
    ap.add_argument("--detect", choices=["audio", "kill", "both"], default="audio",
                    help="探し方: audio=音量 / kill=キル表示(スプラトゥーン3) / both=両方")
    ap.add_argument("--dry-run", action="store_true", help="切り出しは行わず、候補の一覧表示のみ")
    ap.add_argument("--refresh-audio", action="store_true", help="抽出済みWAVを使わず作り直す")
    args = ap.parse_args()

    if shutil.which(tool("ffmpeg")) is None:
        sys.exit("ffmpeg が見つかりません。準備.command(Windowsは準備.bat)を実行してください")
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

    # 1) 検出(音量 / キル表示 / 両方)
    times = []
    if args.detect in ("audio", "both"):
        wav = cache_wav_path(args.video, args.cache_dir)
        extract_audio(args.video, wav, args.track, args.refresh_audio)
        times += detect_peaks(wav, CONFIG["win_sec"], args.percentile)
    if args.detect in ("kill", "both"):
        dur = probe_duration(args.video)
        if dur is None:
            sys.exit("動画の長さを取得できませんでした")
        print("[kill] キル表示を走査中...(初回のみ。動画1時間あたり3〜4分)")
        kills = scan_kills_cached(
            args.video, args.cache_dir, dur,
            on_progress=lambda p: print(f"\r[kill] {p:4.0%}", end="", flush=True),
        )
        print()
        if kills is None:
            sys.exit("キル表示を走査できませんでした(assets/taoshita_1080p.png はありますか?)")
        print(f"[kill] キル表示: {len(kills)}箇所")
        times += kills
    if not times:
        sys.exit("何も検出されませんでした。--percentile を下げるか、--detect を変えて再実行してみてください")

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
        ffmpeg("-ss", f"{s:.1f}", "-i", args.video, "-t", f"{e - s:.1f}",
               *CUT_MAP, "-c", "copy", out)

    print(f"\n完了! {len(clips)} 本のクリップを {args.out_dir}/ に書き出しました")


if __name__ == "__main__":
    main()
