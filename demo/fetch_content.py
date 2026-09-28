#!/usr/bin/env python3
"""下载并整理演示站的媒体内容（docs/design/demo-site.md §4）。

按 demo/content.json 把开放授权的影片与图片整理成 MovieClaw 能直接扫描的目录：

    <out>/电影/Big Buck Bunny (2008) {tmdb-10378}/Big Buck Bunny (2008).mp4
                                                /Big Buck Bunny (2008).简体中文.chs.srt
                                                /movie.nfo          ← 中文简介 + 授权署名
    <out>/图片/动物/小熊猫.jpg                                     ← EXIF 带拍摄时间、去掉 GPS

几条硬规矩（内容合规是演示站的第一要求）：

- **只下清单里的东西**：来源地址写死在 content.json，脚本不做任何搜索或替换；
- **授权二次校验**：图片在下载前向 Wikimedia Commons 重新查询授权，不是 CC0 /
  公有领域就中止——清单写错或图片被改授权都拦得住；
- **字节级锁定**：每个来源文件的 sha256 记进 content.lock.json，之后在任何机器上
  重新下载都必须一致，否则中止（防镜像站被替换内容）。首次运行生成锁文件，
  请把它提交进仓库；
- **只做技术性转换**：影片只换封装（MP4 + faststart，保证网页与 iOS 直接播放、
  不触发服务器转码），音频统一成 AAC 立体声；来源不是 H.264 的才重新编码视频。
  CC BY 协议允许为适配媒介做的技术性修改，简介里仍如实注明。

依赖：Python 3.10+、ffmpeg / ffprobe；处理图片 EXIF 需要 Pillow。MovieClaw 镜像
里这些都有，推荐直接在镜像里跑（见 demo/README.md）。

用法：
    python3 demo/fetch_content.py --out /srv/movieclaw-demo/media
    python3 demo/fetch_content.py --out ./media --only photos
    python3 demo/fetch_content.py --credits demo/CREDITS.md   # 只生成署名清单，不联网
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
USER_AGENT = "MovieClawDemoFetcher/1.0 (+https://github.com/movieclaw/movieclaw)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
# Commons 上允许进演示站的图片授权：只收无附加条件的
PHOTO_LICENSES = {"CC0", "Public domain"}
# 下载分块与重试
CHUNK = 1024 * 1024
RETRIES = 6

# ffprobe 的三字母语言码 → MovieClaw 字幕文件名认得的语言 token
# （services/library/subtitles.py 的 LANGUAGE_TOKENS）；认不得的原样保留当标题
_SUB_LANG = {
    "eng": "en",
    "ger": "de",
    "deu": "de",
    "spa": "es",
    "fre": "fr",
    "fra": "fr",
    "ita": "it",
    "por": "pt",
    "rus": "ru",
    "jpn": "ja",
    "kor": "ko",
    "chi": "chs",
    "zho": "chs",
    "dut": "Nederlands",
    "nld": "Nederlands",
    "pol": "Polski",
    "vie": "Tiếng Việt",
}


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def fail(message: str) -> None:
    log(f"错误：{message}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# 下载与校验
# ---------------------------------------------------------------------------


def _request(url: str, headers: dict[str, str] | None = None) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})


def download(url: str, dest: Path) -> Path:
    """断点续传下载到 dest（先写 .part，完成后改名）。已存在的完整文件直接复用。"""
    if dest.exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    for attempt in range(1, RETRIES + 1):
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with urllib.request.urlopen(_request(url, headers), timeout=60) as resp:
                if have and resp.status != 206:
                    # 服务器不支持续传：从头再来
                    have = 0
                    part.unlink(missing_ok=True)
                total = resp.headers.get("Content-Length")
                total_bytes = int(total) + have if total else None
                mode = "ab" if have else "wb"
                last_report = time.monotonic()
                with part.open(mode) as fh:
                    while chunk := resp.read(CHUNK):
                        fh.write(chunk)
                        have += len(chunk)
                        if time.monotonic() - last_report > 10:
                            last_report = time.monotonic()
                            pct = f"{have * 100 // total_bytes}%" if total_bytes else "?"
                            log(f"  下载中 {dest.name}：{have // CHUNK} MB（{pct}）")
            part.rename(dest)
            return dest
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 416 and part.exists():
                # 已经下完（Range 越界）：直接收尾
                part.rename(dest)
                return dest
            wait = min(2**attempt, 60)
            log(f"  下载失败（第 {attempt}/{RETRIES} 次）：{exc}；{wait} 秒后重试")
            time.sleep(wait)
    fail(f"下载失败：{url}")
    raise AssertionError  # 不可达，给类型检查看


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class Lock:
    """content.lock.json：来源文件的字节指纹。首次记录，之后严格比对。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = json.loads(path.read_text("utf-8")) if path.exists() else {}
        self.dirty = False

    def verify(self, key: str, file: Path, url: str) -> None:
        """按下载到的字节比对（影片、字幕：来源文件是静态的）。"""
        self._check(key, "sha256", sha256_of(file), url)

    def verify_value(self, key: str, field: str, value: str, url: str) -> None:
        """按来源方给出的指纹比对（图片：锁 Commons 原图的 sha1，而不是缩略图字节——
        缩略图会随 Wikimedia 重新渲染而变，原图一旦被替换 sha1 必变）。"""
        self._check(key, field, value, url)

    def _check(self, key: str, field: str, value: str, url: str) -> None:
        known = self.data.get(key)
        if known is None:
            self.data[key] = {"url": url, field: value}
            self.dirty = True
            log(f"  已记录指纹 {key}：{value[:16]}…（首次下载，请提交 content.lock.json）")
            return
        if known.get(field) != value:
            fail(
                f"{key} 的来源与锁定指纹不一致（期望 {str(known.get(field))[:16]}…，"
                f"实际 {value[:16]}…）。来源内容可能被替换，已中止；"
                f"确认新内容合规后删掉 content.lock.json 里的这一项再重跑"
            )

    def save(self) -> None:
        if self.dirty:
            self.path.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            log(f"指纹已写入 {self.path}")


# ---------------------------------------------------------------------------
# 影片
# ---------------------------------------------------------------------------


def safe_name(title: str) -> str:
    """文件名里不能出现的字符换掉（冒号在 macOS / SMB 上会出问题）。"""
    return re.sub(r'[\\/:*?"<>|]+', " -", title).replace("  ", " ").strip()


def ffprobe_streams(ffprobe: str, path: Path) -> list[dict]:
    out = subprocess.run(
        [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out).get("streams", [])


def decode_subtitle(raw: bytes) -> str:
    """字幕统一转成 UTF-8：官方字幕文件编码不一（Sintel 的简中是 GB18030）。"""
    for encoding in ("utf-8-sig", "gb18030", "big5", "cp1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


_SRT_TIME = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})(?:[,.](\d{1,3}))?")
_SRT_TIMING = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)")


def _srt_time(value: str) -> str | None:
    match = _SRT_TIME.fullmatch(value)
    if match is None:
        return None
    hours, minutes, seconds, millis = match.groups()
    return f"{int(hours):02d}:{minutes}:{seconds},{(millis or '0').ljust(3, '0')}"


def normalize_srt(text: str) -> str:
    """把排版不规范的 SRT 规整成标准格式。

    官方的 Tears of Steel 中文字幕每行之间都多一个空行、首条缺序号、时间轴被截断
    （``00:00:06`` 少了毫秒），多数播放器会整份解析失败。这里以时间轴行为锚重建：
    时间轴之后到下一条时间轴之前的非空行是字幕文字（紧挨着下一条时间轴的纯数字行
    是序号，丢掉），序号重新编排。规范的 SRT 过一遍结果不变。
    """
    cues: list[tuple[str, str, list[str]]] = []
    for raw in text.split("\n"):
        line = raw.strip()
        timing = _SRT_TIMING.match(line)
        start = _srt_time(timing.group(1)) if timing else None
        end = _srt_time(timing.group(2)) if timing else None
        if start and end:
            if cues:
                body = cues[-1][2]
                while body and body[-1].isdigit():
                    body.pop()  # 上一条末尾的纯数字行其实是这一条的序号
            cues.append((start, end, []))
        elif line and cues:
            cues[-1][2].append(line)
    blocks = [
        f"{index}\n{start} --> {end}\n" + "\n".join(body)
        for index, (start, end, body) in enumerate(((s, e, b) for s, e, b in cues if b), start=1)
    ]
    return "\n\n".join(blocks) + "\n"


def nfo_for(film: dict, transcoded: bool) -> str:
    """条目 NFO：只写简介与 TMDB 身份——片名、海报、演职员仍以 TMDB 为准。

    简介末尾附授权署名：CC BY 要求在合理位置注明作者、授权与是否修改，
    详情页的简介是访客一定会看到的地方。
    """
    change = "为网页播放重新编码了视频" if transcoded else "仅为网页播放转换了封装格式"
    # 详情页的简介不保留换行：署名写成一行、用「·」分隔，合并后也好读
    plot = (
        f"{film['plot']}\n\n"
        f"授权：{film['license']}（{film['license_url']}）· "
        f"署名：{film['attribution']} · "
        f"来源：{film['homepage']}（本站{change}，内容未作改动）"
    )

    def esc(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        "<movie>\n"
        f"  <plot>{esc(plot)}</plot>\n"
        f'  <uniqueid type="tmdb" default="true">{film["tmdb_id"]}</uniqueid>\n'
        f"  <tmdbid>{film['tmdb_id']}</tmdbid>\n"
        "</movie>\n"
    )


def prepare_film(film: dict, *, out: Path, cache: Path, lock: Lock, tools: dict) -> None:
    library = next(lib for lib in CONTENT["libraries"] if lib["name"] == film["library"])
    stem = f"{safe_name(film['title'])} ({film['year']})"
    folder = out / library["dir"] / f"{stem} {{tmdb-{film['tmdb_id']}}}"
    target = folder / f"{stem}.mp4"
    if target.exists() and (folder / "movie.nfo").exists():
        log(f"跳过 {stem}：已整理过")
        return
    log(f"影片 {stem}（{film['license']}，来自 {film['source']['mirror_of']}）")

    source = film["source"]
    url = source["url"]
    raw = download(url, cache / "films" / urllib.parse.unquote(url.rsplit("/", 1)[-1]))
    lock.verify(f"film:{film['id']}", raw, url)

    media = raw
    if raw.suffix == ".zip":
        with zipfile.ZipFile(raw) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            wanted = source.get("member")
            suffix = source.get("member_suffix")
            member = (
                wanted
                if wanted in names
                else next((n for n in names if suffix and n.lower().endswith(suffix)), None)
            )
            if member is None:
                fail(f"压缩包 {raw.name} 里找不到影片文件（候选：{names[:5]}）")
            media = cache / "films" / Path(member).name
            if not media.exists():
                log(f"  解压 {member}")
                with archive.open(member) as src, media.open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)

    streams = ffprobe_streams(tools["ffprobe"], media)
    video = next((s for s in streams if s["codec_type"] == "video"), None)
    audio = next((s for s in streams if s["codec_type"] == "audio"), None)
    if video is None or audio is None:
        fail(f"{media.name} 缺少视频或音频轨")

    transcode = bool(source.get("transcode")) or not (
        video["codec_name"] == "h264" and video.get("pix_fmt") == "yuv420p"
    )
    if transcode:
        log("  来源不是 H.264：重新编码视频（libx264，最高 1080p），耗时较长")
        video_args = [
            "-c:v", "libx264", "-preset", "slow", "-crf", "20",
            "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-vf", "scale=-2:'min(1080,ih)'",
        ]  # fmt: skip
    else:
        video_args = ["-c:v", "copy"]
    if audio["codec_name"] == "aac" and int(audio.get("channels") or 2) <= 2:
        audio_args = ["-c:a", "copy"]
    else:
        audio_args = ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]

    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{stem}.tmp.mp4"
    cmd = [
        tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
        "-map", "0:v:0", "-map", "0:a:0", *video_args, *audio_args,
        "-sn", "-dn", "-map_chapters", "0", "-movflags", "+faststart", str(tmp),
    ]  # fmt: skip
    log("  转换封装：" + ("视频重编码 + " if transcode else "视频直拷 + ") + " ".join(audio_args))
    subprocess.run(cmd, check=True)

    # 内封字幕抽成外挂 SRT：MP4 放不下 SRT，外挂字幕 MovieClaw 会自动认
    if film.get("embedded_subtitles"):
        subs = [s for s in streams if s["codec_type"] == "subtitle"]
        for index, sub in enumerate(subs):
            lang = (sub.get("tags") or {}).get("language", f"sub{index}")
            token = _SUB_LANG.get(lang, lang)
            dest = folder / f"{stem}.{token}.srt"
            subprocess.run(
                [tools["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y", "-i", str(media),
                 "-map", f"0:s:{index}", "-c:s", "srt", str(dest)],
                check=True,
            )  # fmt: skip
        log(f"  抽出内封字幕 {len(subs)} 条")

    for sub in film.get("subtitles", []):
        sub_name = sub["url"].rsplit("/", 1)[-1]
        raw_sub = download(sub["url"], cache / "subtitles" / film["id"] / sub_name)
        lock.verify(f"subtitle:{film['id']}:{sub['tag']}", raw_sub, sub["url"])
        (folder / f"{stem}.{sub['tag']}.srt").write_text(
            normalize_srt(decode_subtitle(raw_sub.read_bytes())), encoding="utf-8"
        )
    if film.get("subtitles"):
        log(f"  外挂字幕 {len(film['subtitles'])} 条（已统一为 UTF-8、规整为标准 SRT）")

    (folder / "movie.nfo").write_text(nfo_for(film, transcode), encoding="utf-8")
    tmp.rename(target)
    log(f"  完成：{target.relative_to(out)}（{target.stat().st_size // CHUNK} MB）")


# ---------------------------------------------------------------------------
# 图片
# ---------------------------------------------------------------------------


def commons_info(files: list[str], width: int) -> dict[str, dict]:
    """批量查询 Commons 的图片地址与授权（每批 50 个，API 上限）。"""
    result: dict[str, dict] = {}
    for start in range(0, len(files), 50):
        batch = files[start : start + 50]
        params = {
            "action": "query",
            "format": "json",
            "titles": "|".join(batch),
            "prop": "imageinfo",
            "iiprop": "url|size|sha1|extmetadata",
            "iiurlwidth": str(width),
            "iiextmetadatafilter": "LicenseShortName|DateTimeOriginal",
        }
        url = f"{COMMONS_API}?{urllib.parse.urlencode(params)}"
        for attempt in range(1, RETRIES + 1):
            try:
                with urllib.request.urlopen(_request(url), timeout=60) as resp:
                    data = json.load(resp)
                break
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                log(f"  查询 Commons 失败（第 {attempt} 次）：{exc}")
                time.sleep(min(2**attempt, 30))
        else:
            fail("查询 Wikimedia Commons 失败")
        normalized = {n["to"]: n["from"] for n in data["query"].get("normalized", [])}
        for page in data["query"]["pages"].values():
            title = normalized.get(page["title"], page["title"])
            if "imageinfo" not in page:
                fail(f"Commons 上找不到图片：{title}")
            result[title] = page["imageinfo"][0]
    return result


def _jpeg_with_exif(jpeg: bytes, exif_bytes: bytes) -> bytes:
    """无损替换 JPEG 的 EXIF 段（APP1），图像数据一个字节不动。"""
    if jpeg[:2] != b"\xff\xd8":
        fail("不是 JPEG 文件")
    out = bytearray(b"\xff\xd8")
    out += b"\xff\xe1" + (len(exif_bytes) + 2).to_bytes(2, "big") + exif_bytes
    i = 2
    while i + 4 <= len(jpeg) and jpeg[i] == 0xFF:
        marker = jpeg[i + 1]
        if marker == 0xDA:  # 扫描数据开始：之后原样拷贝
            break
        length = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        segment = jpeg[i : i + 2 + length]
        if not (marker == 0xE1 and segment[4:10] == b"Exif\x00\x00"):
            out += segment
        i += 2 + length
    out += jpeg[i:]
    return bytes(out)


def album_time(photo: dict, albums: dict[str, str]) -> datetime:
    """演示相册里的拍摄时间：按分类归到 content.json 指定的月份，日与时刻保留原值。

    图片库按月分组（docs/design/library-photo-kind.md），原图的拍摄时间横跨十几年、
    几乎每月一张，墙面会稀疏成一条时间线；归到几个月份后每组才像一本相册。
    """
    original = datetime.fromisoformat(photo["taken_at"])
    month = albums.get(photo["category"])
    if not month:
        return original
    year, mon = (int(part) for part in month.split("-"))
    return original.replace(year=year, month=mon, day=min(original.day, 28))


def fix_photo_exif(data: bytes, photo: dict, taken_at: datetime) -> bytes:
    """写拍摄时间、作者与授权，去掉 GPS。

    Commons 的缩略图常把 EXIF 剥掉，图片库分组又全靠拍摄时间；原图直出接口会把
    EXIF 原样给访客，所以 GPS 必须去掉。
    """
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(data)) as image:
        exif = image.getexif()
    exif.pop(0x8825, None)  # GPSInfo
    exif_ifd = exif.get_ifd(0x8769)
    exif_ifd.pop(0x927C, None)  # MakerNote：厂商私有数据，可能夹带序列号
    taken = taken_at.strftime("%Y:%m:%d %H:%M:%S")
    exif_ifd[0x9003] = taken  # DateTimeOriginal
    exif_ifd[0x9004] = taken  # DateTimeDigitized
    exif[0x0132] = taken  # DateTime
    # Artist / Copyright 是 ASCII 类型的标签：非 ASCII 字符会被写成「?」，先折成拉丁字母
    author = unicodedata.normalize("NFKD", photo["author"]).encode("ascii", "ignore").decode()
    exif[0x013B] = author  # Artist
    exif[0x8298] = f"{photo['license']} - via Wikimedia Commons"  # Copyright
    return _jpeg_with_exif(data, exif.tobytes())


def prepare_photos(*, out: Path, cache: Path, lock: Lock) -> None:
    section = CONTENT["photos"]
    library = next(lib for lib in CONTENT["libraries"] if lib["name"] == section["library"])
    items = section["items"]
    albums = {k: v for k, v in section.get("albums", {}).items() if not k.startswith("_")}
    info = commons_info([p["commons_file"] for p in items], section["width"])
    for photo in items:
        meta = info[photo["commons_file"]]
        license_name = meta.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")
        if license_name not in PHOTO_LICENSES:
            fail(f"{photo['commons_file']} 的授权是「{license_name}」，不是 CC0 / 公有领域，已中止")
        # 原图指纹每次都比对（只用查询结果，不下载）：已整理过的图片也要能发现来源被替换
        lock.verify_value(
            f"photo:{photo['commons_file']}", "sha1", meta["sha1"], meta["descriptionurl"]
        )
        dest = out / library["dir"] / photo["category"] / f"{safe_name(photo['title'])}.jpg"
        if dest.exists():
            continue
        url = meta.get("thumburl") or meta["url"]
        raw = download(url, cache / "photos" / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}.jpg")
        dest.parent.mkdir(parents=True, exist_ok=True)
        taken_at = album_time(photo, albums)
        dest.write_bytes(fix_photo_exif(raw.read_bytes(), photo, taken_at))
        stamp = taken_at.timestamp()
        os.utime(dest, (stamp, stamp))
        log(f"图片 {dest.relative_to(out)}（{license_name}，{photo['author']}）")
        time.sleep(0.5)  # 对 Wikimedia 友好一点


# ---------------------------------------------------------------------------
# 署名清单
# ---------------------------------------------------------------------------


def write_credits(path: Path) -> None:
    lines = [
        "# 演示站内容署名与授权",
        "",
        "> 本文件由 `python3 demo/fetch_content.py --credits demo/CREDITS.md` 从",
        "> `demo/content.json` 生成，请勿手改。演示站只收录开放授权作品：",
        "> 影片均为知识共享署名（CC BY）协议，图片均为 CC0 / 公有领域。",
        "",
        "## 影片",
        "",
        "影片仅为网页与 App 直接播放转换了封装格式（MP4），音频统一为 AAC 立体声；",
        "Spring、The Daily Dweebs 的来源是 WebM，重新编码成了 H.264。画面与声音内容均未改动。",
        "",
        "| 影片 | 年份 | 授权 | 署名 | 官方页面 | 下载来源 |",
        "|---|---|---|---|---|---|",
    ]
    for film in CONTENT["films"]:
        lines.append(
            f"| {film['title']} | {film['year']} | [{film['license']}]({film['license_url']}) "
            f"| {film['attribution']} | {film['homepage']} | {film['source']['mirror_of']} |"
        )
    lines += [
        "",
        "## 图片",
        "",
        "全部来自 Wikimedia Commons 的「精选图片」，授权为 CC0"
        "（无需署名，这里仍列出作者以示感谢）。",
        "演示站去掉了图片的 GPS 信息；为了让相册按月成组，拍摄时间按分类归到了 2026 年的"
        "几个月份（日与时刻不变），下表是 Commons 记录的原始拍摄时间。",
        "",
        "| 图片 | 分类 | 作者 | 授权 | 原始拍摄时间 | 来源 |",
        "|---|---|---|---|---|---|",
    ]
    for photo in CONTENT["photos"]["items"]:
        lines.append(
            f"| {photo['title']} | {photo['category']} | {photo['author']} | {photo['license']} "
            f"| {photo['taken_at'][:10]} | [{photo['commons_file']}]({photo['source_page']}) |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"署名清单已写入 {path}")


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="下载并整理 MovieClaw 演示站的开放授权内容")
    parser.add_argument("--content", type=Path, default=HERE / "content.json")
    parser.add_argument("--lock", type=Path, default=HERE / "content.lock.json")
    parser.add_argument("--out", type=Path, help="媒体根目录（容器里挂载成 /media）")
    parser.add_argument("--cache", type=Path, help="下载缓存目录（默认 <out>/../.demo-cache）")
    parser.add_argument("--only", choices=["films", "photos"], help="只处理影片或只处理图片")
    parser.add_argument("--ids", help="只处理这些影片（逗号分隔的 id）")
    parser.add_argument("--credits", type=Path, help="只生成署名清单到这个路径，不联网")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe")
    args = parser.parse_args()

    global CONTENT
    CONTENT = json.loads(args.content.read_text("utf-8"))

    if args.credits:
        write_credits(args.credits)
        return
    if args.out is None:
        parser.error("需要 --out（媒体根目录）")

    out = args.out.resolve()
    cache = (args.cache or out.parent / ".demo-cache").resolve()
    lock = Lock(args.lock)
    tools = {"ffmpeg": args.ffmpeg, "ffprobe": args.ffprobe}
    try:
        if args.only in (None, "films"):
            wanted = set(args.ids.split(",")) if args.ids else None
            for film in CONTENT["films"]:
                if wanted is None or film["id"] in wanted:
                    prepare_film(film, out=out, cache=cache, lock=lock, tools=tools)
        if args.only in (None, "photos"):
            prepare_photos(out=out, cache=cache, lock=lock)
    finally:
        lock.save()
    log(f"全部完成。媒体目录：{out}；下载缓存可删除：{cache}")


CONTENT: dict = {}

if __name__ == "__main__":
    main()
