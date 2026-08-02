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


# ============ 画面表示マーカー(お手本画像との照合)の検出 ============
# スプラトゥーン3の決まった画面表示を、同梱のお手本画像と照合して時刻を特定する。
#   kill   : 「◯◯をたおした!」(画面下端。ハイライト検出に使う)
#   start  : 「バトルを開始します!」(画面中央。試合の始まりの目印)
#   finish : 「Finish!」のテープ(配置は毎回同じ。試合の終わりの目印)
# 走査は動画を1/3縮小(640x360)でデコードし、1回のデコードで全マーカーを評価する。
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
KILL_TEMPLATE = ASSETS_DIR / "taoshita_1080p.png"   # 後方互換のため残す

SCAN_W, SCAN_H = 640, 360   # 走査時の画面サイズ(1080pの1/3。16:9前提)
SCAN_FPS = 2                # 秒2コマ。開始画面の表示が約1.3秒しかないため

# region は1080p基準の探索範囲 (x, y, w, h)。tmpl_size は1/3縮小後のお手本サイズ。
# threshold は実測にもとづく(表示中/非表示のスコアが十分離れる値)
MARKERS = {
    "kill": {
        "file": "taoshita_1080p.png", "region": (600, 960, 720, 96),
        "tmpl_size": (60, 14), "threshold": 0.8,
    },
    "start": {
        "file": "battle_start_1080p.png", "region": (600, 500, 720, 120),
        "tmpl_size": (113, 13), "threshold": 0.65,
    },
    "finish": {
        "file": "finish_1080p.png", "region": (120, 730, 480, 200),
        "tmpl_size": (120, 40), "threshold": 0.7,
    },
}


def load_marker_template(path: Path, size: tuple[int, int]):
    """お手本画像を照合用の縮小グレースケールとして読み込む"""
    if not path.is_file():
        return None
    w, h = size
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path),
           "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    if len(raw) < w * h:
        return None
    t = np.frombuffer(raw[: w * h], dtype=np.uint8).astype(np.float32).reshape(h, w)
    return (t - t.mean()) / (t.std() + 1e-6)


def _match_score(frame, tmpl) -> float:
    """探索範囲の中でお手本と一番似ている場所の一致度(-1〜1)。

    位置は2ピクセル刻みで探す(4倍速)。1ピクセルずれても一致度の低下は
    わずかで、しきい値には十分な余裕を取ってある。
    """
    th, tw = tmpl.shape
    if frame.shape[0] < th or frame.shape[1] < tw or frame.std() < 1:
        return 0.0
    w = sliding_window_view(frame, (th, tw))[::2, ::2]
    mu = w.mean(axis=(2, 3), keepdims=True)
    sd = w.std(axis=(2, 3), keepdims=True) + 1e-6
    ncc = np.einsum("ijkl,kl->ij", (w - mu) / sd, tmpl) / tmpl.size
    return float(ncc.max())


def scan_markers(video: Path, duration: float, chunk: float = 120.0,
                 on_progress=None) -> dict[str, list[float]] | None:
    """全マーカーの出現時刻をまとめて走査する。走査できない場合は None。

    表示が続いている間は最初の1コマだけを記録する。処理時間はデコードが
    ほぼすべてなので、マーカーが増えても走査時間は変わらない。
    """
    tmpls = {}
    for name, m in MARKERS.items():
        t = load_marker_template(ASSETS_DIR / m["file"], m["tmpl_size"])
        if t is None:
            return None
        tmpls[name] = t

    events: dict[str, list[float]] = {n: [] for n in MARKERS}
    prev = {n: False for n in MARKERS}
    t0 = 0.0
    while t0 < duration:
        d = min(chunk, duration - t0)
        cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", str(t0),
               "-i", str(video), "-t", str(d),
               "-vf", f"fps={SCAN_FPS},scale={SCAN_W}:{SCAN_H},format=gray",
               "-f", "rawvideo", "-"]
        raw = subprocess.run(cmd, capture_output=True).stdout
        n = len(raw) // (SCAN_W * SCAN_H)
        frames = np.frombuffer(raw[: n * SCAN_W * SCAN_H], dtype=np.uint8) \
            .reshape(n, SCAN_H, SCAN_W).astype(np.float32)
        for i, f in enumerate(frames):
            ts = t0 + i / SCAN_FPS
            for name, m in MARKERS.items():
                x, y, w, h = (v // 3 for v in m["region"])
                hit = _match_score(f[y:y + h, x:x + w], tmpls[name]) >= m["threshold"]
                if hit and not prev[name]:
                    events[name].append(ts)
                prev[name] = hit
        t0 += chunk
        if on_progress:
            on_progress(min(1.0, t0 / duration))
    return events


def markers_cache_path(video: Path, cache_dir: Path) -> Path:
    """マーカー走査の結果キャッシュ(JSON)のパス"""
    return cache_path(video, cache_dir, "markers", "json")


def scan_markers_cached(video: Path, cache_dir: Path, duration: float,
                        on_progress=None, refresh: bool = False) -> dict[str, list[float]] | None:
    """マーカー走査の結果をキャッシュ付きで返す(走査は動画1時間あたり3〜4分かかるため)"""
    p = markers_cache_path(video, cache_dir)
    if p.exists() and not refresh:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            mk = d.get("markers")
            if isinstance(mk, dict) and all(isinstance(mk.get(n), list) for n in MARKERS):
                return {n: [float(t) for t in mk[n]] for n in MARKERS}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass  # 壊れた・古い形式のキャッシュは作り直す
    events = scan_markers(video, duration, on_progress=on_progress)
    if events is None:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 1, "markers": events}), encoding="utf-8")
    return events


# ============ リザルト画面の読み取り(勝敗・キル数・デス数) ============
# Finishの後に出る画面から成績を読む。
#   win/lose : 画面左上の「WIN!」「LOSE...」
#   result   : 「ゲットした表彰」画面。ここの固定位置に「x10」のような白い2桁数字で
#              キル数・デス数が出る(アイコンの色はインク色で変わるが数字は常に白)
RESULT_SCREENS = {
    "win":    {"file": "win_1080p.png",    "region": (0, 0, 480, 240),     "tmpl_size": (103, 47), "threshold": 0.7},
    "lose":   {"file": "lose_1080p.png",   "region": (0, 0, 480, 240),     "tmpl_size": (103, 47), "threshold": 0.7},
    "result": {"file": "result_1080p.png", "region": (780, 330, 480, 150), "tmpl_size": (120, 30), "threshold": 0.7},
}
KD_STRIP = (1488, 265, 272, 70)   # 「x15 x02 (x06)」が並ぶ帯(1080p)。上に重なるアイコンも含む
DIGIT_W, DIGIT_H = 16, 24   # 数字1文字の正規化サイズ
DIGITS_SHEET = ASSETS_DIR / "digits_1080p.png"   # 0〜9を横に並べた見本シート


def _result_templates():
    out = {}
    for name, m in RESULT_SCREENS.items():
        t = load_marker_template(ASSETS_DIR / m["file"], m["tmpl_size"])
        if t is None:
            return None
        out[name] = t
    return out


def scan_result_screens(video: Path, finish_t: float, span: float = 75.0):
    """Finish後の画面から、勝敗と「表彰」画面の時刻を探す"""
    tmpls = _result_templates()
    if tmpls is None:
        return None
    t0 = finish_t + 3.0
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", str(t0),
           "-i", str(video), "-t", str(span),
           "-vf", f"fps=1,scale={SCAN_W}:{SCAN_H},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    n = len(raw) // (SCAN_W * SCAN_H)
    frames = np.frombuffer(raw[: n * SCAN_W * SCAN_H], dtype=np.uint8) \
        .reshape(n, SCAN_H, SCAN_W).astype(np.float32)

    outcome, t_result = None, None
    for i, f in enumerate(frames):
        ts = t0 + i
        scores = {}
        for name, m in RESULT_SCREENS.items():
            x, y, w, h = (v // 3 for v in m["region"])
            scores[name] = _match_score(f[y:y + h, x:x + w], tmpls[name])
        if outcome is None:
            if scores["win"] >= RESULT_SCREENS["win"]["threshold"] and scores["win"] > scores["lose"]:
                outcome = True
            elif scores["lose"] >= RESULT_SCREENS["lose"]["threshold"] and scores["lose"] > scores["win"]:
                outcome = False
        if t_result is None and scores["result"] >= RESULT_SCREENS["result"]["threshold"]:
            t_result = ts
        if outcome is not None and t_result is not None:
            break
    return {"win": outcome, "t_result": t_result}


def _normalize_glyph(white: np.ndarray):
    rows = white.sum(axis=1).nonzero()[0]
    if len(rows) < 5:
        return None
    im = white[rows[0]:rows[-1] + 1].astype(np.float32)
    yi = (np.arange(DIGIT_H) * im.shape[0] / DIGIT_H).astype(int)
    xi = (np.arange(DIGIT_W) * im.shape[1] / DIGIT_W).astype(int)
    return im[yi][:, xi]


def _col_blobs(white: np.ndarray, min_w: int = 2):
    """白が続く列の塊 [(x0, x1), ...] を返す"""
    cols = white.sum(axis=0)
    blobs, start = [], None
    for i, c in enumerate(list(cols) + [0]):
        if c > 0 and start is None:
            start = i
        elif c == 0 and start is not None:
            if i - start >= min_w:
                blobs.append((start, i))
            start = None
    return blobs


def parse_kd_strip(strip_gray: np.ndarray) -> list[list[np.ndarray]]:
    """「x15 x02 …」の帯から、各項目の数字グリフ(2文字)を取り出す。

    帯の上にはアイコンが重なっているので、まず「一番下の文字の行帯」を探し、
    その中で列ごとに文字を切る。各項目は「x」+2桁で、xは数字より背が低いので除く。
    """
    white = strip_gray > 180
    # 行方向の帯を探す(2行以上の空白で区切る)。数字は一番下の帯にある
    rows = white.sum(axis=1)
    bands, start = [], None
    for i, r in enumerate(list(rows) + [0, 0]):
        if r > 0 and start is None:
            start = i
        elif r == 0 and start is not None:
            if i - start >= 14:
                bands.append((start, i))
            start = None
    if not bands:
        return []
    y0, y1 = bands[-1]
    band = white[y0:y1]
    band_h = y1 - y0

    # 列の塊 → 間隔15px以上で項目(キル/デス/スペシャル)に分ける
    blobs = _col_blobs(band)
    groups, cur = [], []
    for b in blobs:
        if cur and b[0] - cur[-1][1] >= 15:
            groups.append(cur)
            cur = []
        cur.append(b)
    if cur:
        groups.append(cur)

    items = []
    for g in groups:
        # 「x」は数字より背が低いので除外。残りが数字
        digits = []
        for x0, x1 in g:
            sub = band[:, x0:x1]
            r = sub.sum(axis=1).nonzero()[0]
            h = (r[-1] - r[0] + 1) if len(r) else 0
            if h >= band_h * 0.75:
                digits.append((x0, x1))
        # 2桁がくっついて1塊になった場合は真ん中で割る
        if len(digits) == 1 and digits[0][1] - digits[0][0] >= 20:
            x0, x1 = digits[0]
            mid = (x0 + x1) // 2
            digits = [(x0, mid), (mid, x1)]
        glyphs = [n for x0, x1 in digits[:2]
                  if (n := _normalize_glyph(band[:, x0:x1])) is not None]
        items.append(glyphs)
    return items


def _load_digit_sheet():
    """0〜9の見本シートを読み込む(無ければ None)"""
    if not DIGITS_SHEET.is_file():
        return None
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(DIGITS_SHEET),
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    try:
        raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    if len(raw) < DIGIT_W * 10 * DIGIT_H:
        return None
    sheet = np.frombuffer(raw[: DIGIT_W * 10 * DIGIT_H], dtype=np.uint8) \
        .reshape(DIGIT_H, DIGIT_W * 10).astype(np.float32)
    return [sheet[:, i * DIGIT_W:(i + 1) * DIGIT_W] / 255.0 for i in range(10)]


def _classify_digit(glyph: np.ndarray, sheet) -> int | None:
    best, best_d = -1.0, None
    for d, ref in enumerate(sheet):
        if ref.max() < 0.5:   # シートに無い数字(空欄)
            continue
        a = glyph - glyph.mean()
        b = ref - ref.mean()
        s = float((a * b).sum() / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))
        if s > best:
            best, best_d = s, d
    return best_d if best > 0.5 else None


def kd_strip_at(video: Path, t: float) -> np.ndarray | None:
    """「表彰」画面のフレームから数字の帯を切り出す"""
    x, y, w, h = KD_STRIP
    cmd = [tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-ss", str(t),
           "-i", str(video), "-frames:v", "1",
           "-vf", f"scale=1920:1080,format=gray,crop={w}:{h}:{x}:{y}", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True).stdout
    if len(raw) < w * h:
        return None
    return np.frombuffer(raw[: w * h], dtype=np.uint8).reshape(h, w).astype(np.float32)


def read_kd(video: Path, t: float) -> dict:
    """「表彰」画面のフレームからキル数・デス数を読む。読めなければ None を入れる。

    画面の表示時間は操作しだいで短いことがあるので、前後の数フレームを順に試す。
    """
    out = {"kills": None, "deaths": None}
    sheet = _load_digit_sheet()
    if sheet is None:
        return out
    for dt in (1.0, 0.5, 0.0, 1.5, 2.5):
        strip = kd_strip_at(video, t + dt)
        if strip is None:
            continue
        items = parse_kd_strip(strip)
        if len(items) >= 2 and len(items[0]) == 2 and len(items[1]) == 2:
            got = {}
            for key, glyphs in zip(("kills", "deaths"), items):
                d1 = _classify_digit(glyphs[0], sheet)
                d2 = _classify_digit(glyphs[1], sheet)
                if d1 is not None and d2 is not None:
                    got[key] = d1 * 10 + d2
            if len(got) == 2:
                return got
    return out


def analyze_matches(video: Path, matches, kills, on_progress=None) -> list[dict]:
    """各試合の成績(勝敗・キル数・デス数・キル表示回数)を調べる"""
    rows = []
    for i, (s, e) in enumerate(matches):
        finish_t = e - 4.0   # pair_matches が Finish+4秒 を終端にしている
        res = scan_result_screens(video, finish_t) or {}
        kd = read_kd(video, res["t_result"] + 1.0) if res.get("t_result") else {}
        shown = sum(1 for t in kills if s <= t <= e)
        rows.append({
            "start": s, "end": e, "win": res.get("win"),
            "kills": kd.get("kills"), "deaths": kd.get("deaths"),
            "kills_shown": shown,
        })
        if on_progress:
            on_progress((i + 1) / len(matches))
    return rows


def results_cache_path(video: Path, cache_dir: Path) -> Path:
    return cache_path(video, cache_dir, "results", "json")


def analyze_matches_cached(video: Path, cache_dir: Path, matches, kills,
                           on_progress=None, refresh: bool = False) -> list[dict]:
    """試合分析の結果をキャッシュ付きで返す(試合数×数秒かかるため)"""
    p = results_cache_path(video, cache_dir)
    key = [round(float(s), 1) for s, _ in matches]
    if p.exists() and not refresh:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if d.get("key") == key and isinstance(d.get("rows"), list):
                return d["rows"]
        except (OSError, json.JSONDecodeError):
            pass
    rows = analyze_matches(video, matches, kills, on_progress=on_progress)
    cache_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"key": key, "rows": rows}), encoding="utf-8")
    return rows


def pair_matches(starts, finishes, duration: float,
                 lead: float = 2.0, tail: float = 4.0) -> list[list[float]]:
    """開始/Finishの時刻から試合区間を組み立てる。

    「開始 → 次のFinish」を1試合とする。開始が2回続いた場合(回線落ちなどで
    Finishが無い試合)は、後の開始を採用して前を捨てる。
    """
    events = sorted([(t, 0) for t in starts] + [(t, 1) for t in finishes])
    matches = []
    cur = None
    for t, kind in events:
        if kind == 0:
            cur = t
        elif cur is not None and t > cur:
            matches.append([max(0.0, cur - lead), min(duration, t + tail)])
            cur = None
    return matches


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


def merge_intervals_within(times, spans, before, after, gap) -> list[list[float]]:
    """検出時刻を試合区間の中だけでクリップ化する。

    区間ごとに merge_intervals を適用し、クリップが試合の外(ロビー画面など)へ
    はみ出さないよう区間の端で切り詰める。
    """
    out = []
    for s, e in spans:
        inside = [t for t in times if s <= t <= e]
        for c0, c1 in merge_intervals(inside, before, after, gap):
            out.append([max(c0, s), min(c1, e)])
    return out


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
        markers = scan_markers_cached(
            args.video, args.cache_dir, dur,
            on_progress=lambda p: print(f"\r[kill] {p:4.0%}", end="", flush=True),
        )
        print()
        if markers is None:
            sys.exit("キル表示を走査できませんでした(assets/ のお手本画像はありますか?)")
        print(f"[kill] キル表示: {len(markers['kill'])}箇所")
        times += markers["kill"]
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
