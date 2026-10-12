#!/usr/bin/env python3
"""Documentary builder: Pixabay footage + edge-tts narration + ffmpeg editing.

Everything is timed from the narration: each chapter's length is the sum of its
spoken sentences (+gaps), rounded up to a whole number of frames. Footage is cut
to that exact length, subtitles use the exact sentence times, and chapters are
concatenated frame-exact, so picture, voice and captions stay aligned.
"""
import asyncio
import json
import math
import os
import pathlib
import re
import sys
import time

import edge_tts
import requests
import subprocess

KEY = os.environ["PIXABAY_API_KEY"]
W, H, FPS = 1920, 1080, 30
XF = 0.8        # cross-dissolve length between shots (s)
GAP = 0.45      # pause between spoken sentences (s)
FADE = 0.6      # dip-to-black at chapter boundaries (s)
SHOT = 9.0      # target shot length (s)
VOICE = os.environ.get("VOICE", "en-US-GuyNeural")
RATE = os.environ.get("RATE", "-6%")

WORK = pathlib.Path("work").resolve()
OUT = pathlib.Path("out").resolve()
for d in ("clips", "tts", "shots", "chapters"):
    (WORK / d).mkdir(parents=True, exist_ok=True)
OUT.mkdir(exist_ok=True)

FALLBACK = ["galaxy", "nebula space", "stars universe", "milky way", "planet space",
            "astronomy", "supernova", "space"]


def run(cmd):
    p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if p.returncode:
        sys.exit("FAILED: " + " ".join(map(str, cmd)) + "\n" + p.stderr[-2500:])
    return p.stdout


def ff(*a):
    return run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *a])


def dur(path):
    return float(run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                      "-of", "default=nw=1:nk=1", path]).strip())


# ----------------------------------------------------------------- narration
async def synth_all(jobs):
    sem = asyncio.Semaphore(4)

    async def one(text, path):
        if path.exists() and path.stat().st_size > 1000:
            return
        async with sem:
            for i in range(6):
                try:
                    await edge_tts.Communicate(text, VOICE, rate=RATE).save(str(path))
                    if path.stat().st_size > 1000:
                        return
                except Exception as e:  # network hiccup / throttling
                    print("tts retry:", e)
                    await asyncio.sleep(2 * (i + 1))
            sys.exit("TTS failed for: " + text[:60])

    await asyncio.gather(*(one(t, p) for t, p in jobs))


def silence(sec):
    p = WORK / "tts" / f"sil_{int(sec * 1000)}.wav"
    if not p.exists():
        ff("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", f"{sec:.3f}",
           "-c:a", "pcm_s16le", p)
    return p


def build_narration(chapters):
    jobs, plan = [], []
    for ci, ch in enumerate(chapters):
        sents = [s for s in re.split(r"(?<=[.!?])\s+", ch["text"].strip()) if s]
        paths = []
        for si, s in enumerate(sents):
            p = WORK / "tts" / f"c{ci}_{si}.mp3"
            jobs.append((s, p))
            paths.append(p)
        plan.append((sents, paths))
    asyncio.run(synth_all(jobs))

    results = []
    for ci, ch in enumerate(chapters):
        sents, paths = plan[ci]
        parts, timing, t = [silence(ch["head"])], [], ch["head"]
        for s, mp3 in zip(sents, paths):
            wav = mp3.with_suffix(".wav")
            if not wav.exists():
                ff("-i", mp3, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", wav)
            d = dur(wav)
            timing.append((t, t + d, s))
            parts += [wav, silence(GAP)]
            t += d + GAP
        parts.append(silence(ch["tail"]))
        lst = WORK / f"c{ci}_list.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in parts))
        raw = WORK / f"c{ci}_raw.wav"
        ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", raw)
        nf = math.ceil((dur(raw) + ch["tail"] * 0) * FPS)
        T = nf / FPS
        wav = WORK / f"c{ci}_narr.wav"
        ff("-i", raw, "-af", f"apad=whole_dur={T:.6f}", "-t", f"{T:.6f}",
           "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", wav)
        results.append({"T": T, "nf": nf, "wav": wav, "timing": timing})
        print(f"chapter {ci + 1}: {T:.1f}s, {len(sents)} sentences")
    return results


# ------------------------------------------------------------------- footage
_cache = {}


def search(q):
    if q in _cache:
        return _cache[q]
    time.sleep(0.8)  # stay well inside Pixabay's rate limit
    r = requests.get("https://pixabay.com/api/videos/",
                     params={"key": KEY, "q": q, "per_page": 50, "safesearch": "true"},
                     timeout=30)
    r.raise_for_status()
    hits = [h for h in r.json().get("hits", []) if h.get("duration", 0) >= 6]
    _cache[q] = hits
    return hits


def video_url(hit):
    vids = hit.get("videos", {})
    for k in ("large", "medium"):
        v = vids.get(k)
        if v and v.get("url") and v.get("width", 0) >= 1280:
            return v["url"]
    for k in ("large", "medium", "small", "tiny"):
        if vids.get(k, {}).get("url"):
            return vids[k]["url"]
    return None


def download(hit):
    path = WORK / "clips" / f"{hit['id']}.mp4"
    if path.exists() and path.stat().st_size > 10000:
        return path
    with requests.get(video_url(hit), stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return path


def pick_clips(queries, n, used):
    lists = [list(search(q)) for q in queries]
    chosen, i = [], 0
    while len(chosen) < n and any(lists):
        lst = lists[i % len(lists)]
        i += 1
        while lst and (lst[0]["id"] in used or not video_url(lst[0])):
            lst.pop(0)
        if lst:
            h = lst.pop(0)
            used.add(h["id"])
            chosen.append(h)
    if len(chosen) < n:  # generic space footage as filler
        for q in FALLBACK:
            for h in search(q):
                if h["id"] not in used and video_url(h):
                    used.add(h["id"])
                    chosen.append(h)
                    if len(chosen) >= n:
                        return chosen
    if not chosen:
        sys.exit("No Pixabay footage found - check the API key")
    while len(chosen) < n:  # last resort: reuse
        chosen.append(chosen[len(chosen) % max(1, len(chosen))])
    return chosen


def render_shot(src, L, out, k):
    pan = f"t/{L:.3f}" if k % 2 == 0 else f"(1-t/{L:.3f})"
    vf = (f"scale=2112:1188:force_original_aspect_ratio=increase,"
          f"crop=w={W}:h={H}:x='(in_w-{W})*{pan}':y='(in_h-{H})/2',"
          f"fps={FPS},eq=contrast=1.06:saturation=0.85:brightness=-0.03,"
          f"vignette=PI/5,format=yuv420p,setsar=1")
    ff("-stream_loop", "-1", "-i", src, "-t", f"{L:.3f}", "-vf", vf, "-an",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", out)


def build_chapter_video(ci, ch, info, used):
    T, nf = info["T"], info["nf"]
    n = max(1, round(T / SHOT))
    base = (T - XF) / n if n > 1 else T
    L = base + XF if n > 1 else T
    clips = pick_clips(ch["queries"], n, used)
    shots = []
    for k, h in enumerate(clips):
        out = WORK / "shots" / f"c{ci}_{k}.mp4"
        render_shot(download(h), L, out, k)
        shots.append(out)
    if n == 1:
        chain = "[0:v]null[vx]"
    else:
        steps, prev = [], "[0:v]"
        for k in range(1, n):
            lab = f"[x{k}]" if k < n - 1 else "[vx]"
            steps.append(f"{prev}[{k}:v]xfade=transition=fade:duration={XF}:offset={k * base:.3f}{lab}")
            prev = lab
        chain = ";".join(steps)
    fc = (f"{chain};[vx]tpad=stop_mode=clone:stop_duration=2,"
          f"fade=t=in:st=0:d={FADE},fade=t=out:st={T - FADE:.3f}:d={FADE}[v]")
    args = []
    for s in shots:
        args += ["-i", s]
    out = WORK / "chapters" / f"c{ci}.mp4"
    ff(*args, "-filter_complex", fc, "-map", "[v]", "-frames:v", nf, "-r", FPS,
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "17", "-pix_fmt", "yuv420p", out)
    return out


# ----------------------------------------------------------------- subtitles
def ts_ass(t):
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ts_srt(t):
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def clean(s):
    return s.replace("{", "(").replace("}", ")").replace("\n", " ")


def write_subs(title, chapters, infos):
    head = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,DejaVu Sans,48,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,3,1,2,220,220,70,1
Style: Chap,DejaVu Sans,40,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,3,1,7,120,120,110,1
Style: Title,DejaVu Sans,60,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,0,0,0,0,100,100,0,0,1,3,2,5,100,100,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev, srt, g, idx = [], [], 0.0, 1
    for ci, (ch, info) in enumerate(zip(chapters, infos)):
        if ci == 0:
            ev.append(f"Dialogue: 1,{ts_ass(0.6)},{ts_ass(5.6)},Title,,0,0,0,,"
                      r"{\fad(1500,1200)}{\fs130\b1}BLACK HOLES{\r}\N{\fs54\fsp10\c&HDCDCDC&}BEYOND THE EVENT HORIZON")
        else:
            ev.append(f"Dialogue: 1,{ts_ass(g + 0.8)},{ts_ass(g + 5.0)},Chap,,0,0,0,,"
                      r"{\fad(600,600)}{\fs30\b1\fsp6\c&H00C8FF&}CHAPTER " + f"{ci + 1:02d}"
                      r"{\r}\N{\fs62\b1}" + clean(ch["title"]))
        for a, b, s in info["timing"]:
            ev.append(f"Dialogue: 0,{ts_ass(g + a)},{ts_ass(g + b)},Sub,,0,0,0,,"
                      r"{\fad(120,120)}" + clean(s))
            srt.append(f"{idx}\n{ts_srt(g + a)} --> {ts_srt(g + b)}\n{s}\n")
            idx += 1
        if ci == len(chapters) - 1:
            end = g + info["T"]
            ev.append(f"Dialogue: 1,{ts_ass(end - ch['tail'] + 0.6)},{ts_ass(end - 0.4)},Title,,0,0,0,,"
                      r"{\fad(1000,1000)}{\fs56\b1}" + clean(title) +
                      r"{\r}\N{\fs30\c&HCCCCCC&}Footage: Pixabay contributors  |  Narration: neural text to speech")
        g += info["T"]
    (WORK / "subs.ass").write_text(head + "\n".join(ev) + "\n", encoding="utf-8")
    (OUT / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")


# --------------------------------------------------------------------- audio
def build_music(total):
    out = WORK / "music.wav"
    fade = f"afade=t=in:d=4,afade=t=out:st={total - 6:.2f}:d=6"
    user = pathlib.Path("assets/music.mp3")
    if user.exists():
        ff("-stream_loop", "-1", "-i", user, "-t", f"{total:.3f}", "-af", fade,
           "-ar", "48000", "-ac", "2", out)
        return out
    fc = ("[0]volume=0.5,tremolo=f=0.07:d=0.6[a];[1]volume=0.4,tremolo=f=0.11:d=0.7[b];"
          "[2]volume=0.25,tremolo=f=0.05:d=0.8[c];[3]volume=0.12,tremolo=f=0.09:d=0.8[d];"
          "[4]lowpass=f=400,volume=0.25[e];"
          "[a][b][c][d][e]amix=inputs=5:normalize=0,volume=3,"
          "aecho=0.8:0.88:700|1300:0.35|0.25,lowpass=f=1200," + fade + "[m]")
    args = []
    for f in ("sine=f=55:r=48000", "sine=f=82.41:r=48000", "sine=f=110.5:r=48000",
              "sine=f=164.8:r=48000", "anoisesrc=c=pink:r=48000:a=0.4"):
        args += ["-f", "lavfi", "-i", f]
    try:
        ff(*args, "-filter_complex", fc, "-map", "[m]", "-t", f"{total:.3f}",
           "-ar", "48000", "-ac", "2", out)
    except SystemExit:  # older ffmpeg without amix normalize option
        ff(*args, "-filter_complex", fc.replace(":normalize=0,volume=3", ",volume=15"),
           "-map", "[m]", "-t", f"{total:.3f}", "-ar", "48000", "-ac", "2", out)
    return out


# ---------------------------------------------------------------------- main
def main():
    spec = json.load(open("script.json", encoding="utf-8"))
    chapters = spec["chapters"]
    infos = build_narration(chapters)
    total = sum(i["T"] for i in infos)
    print(f"Total runtime: {total / 60:.2f} min")
    if not 8.5 * 60 <= total <= 9.5 * 60:
        print("WARNING: runtime is off the 9 minute target - adjust RATE (e.g. -10% / 0%) or edit script.json")

    used, vids = set(), []
    for ci, (ch, info) in enumerate(zip(chapters, infos)):
        vids.append(build_chapter_video(ci, ch, info, used))
        print(f"chapter {ci + 1} video ready")

    lst = WORK / "video_list.txt"
    lst.write_text("".join(f"file '{v}'\n" for v in vids))
    video = WORK / "video.mp4"
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", video)

    lst = WORK / "narr_list.txt"
    lst.write_text("".join(f"file '{i['wav']}'\n" for i in infos))
    narr = WORK / "narration.wav"
    ff("-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", narr)

    music = build_music(total)
    write_subs(spec["title"], chapters, infos)

    final = OUT / "black_holes_beyond_the_event_horizon.mp4"
    fc = ("[0:v]ass=work/subs.ass[v];"
          "[1:a]asplit=2[n1][n2];[2:a]volume=0.55[m];"
          "[m][n1]sidechaincompress=threshold=0.02:ratio=10:attack=30:release=600[d];"
          "[d][n2]amix=inputs=2:duration=first:dropout_transition=0,volume=2,"
          "loudnorm=I=-16:TP=-1.5:LRA=11[a]")
    ff("-i", video, "-i", narr, "-i", music, "-filter_complex", fc,
       "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
       "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
       "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final)
    print(f"Done: {final} ({dur(final) / 60:.2f} min)")


if __name__ == "__main__":
    main()
