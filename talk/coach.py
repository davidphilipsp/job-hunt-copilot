"""Voice interview coach: question → say → record → faster-whisper → flags → (Claude feedback) → practice log."""
from __future__ import annotations

import argparse
import json
import os
import queue
import random
import re
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jobs.finder import CANDIDATE, letter_ok  # noqa: E402  (same no-numbers / banned-words rules)
from jobs import settings  # noqa: E402

PRACTICE = ROOT / "practice"
FLAGS_LOG = PRACTICE / "flags.jsonl"
SAMPLE_RATE = 16000
LOW_PROB = 0.6
# Function words are often low-confidence for everyone; --review skips them so the drill list is useful.
STOPWORDS = set("a an the i am is are was were be to of in on at for and or but so it my me we you he she they "
                "that this with as by do did have has had not".split())

GENERIC = [
    "Tell me about yourself.",
    "Why did you leave your last role?",
    "What's the largest budget you've managed?",
    "How do you use AI in paid media?",
    "Walk me through your weekly creative testing process.",
    "How do you set up tracking so the platform optimizes to the right event?",
    "Tell me about a launch that didn't go as planned and what you changed.",
    "How do you work with a sales or SDR team on lead quality?",
    "How do you connect paid acquisition with CRM and retention?",
    "What salary are you looking for?",
]


# ---------------------------------------------------------------- questions


def latest_report() -> Path | None:
    reports = sorted((ROOT / "out").glob("*.md"))
    return reports[-1] if reports else None


def top_job() -> dict | None:
    rep = latest_report()
    if not rep:
        return None
    txt = rep.read_text()
    m = re.search(r"^## 1\. (.+?) — (.+?)  ·", txt, re.M)
    if not m:
        return None
    block = txt[m.start(): txt.find("\n## 2.", m.start()) if "\n## 2." in txt else len(txt)]
    gap = re.search(r"\*\*Gap to address:\*\* (.+?)\. \*(.+?)\*", block)
    lane = re.search(r"\*\*Lane:\*\* (\w+)\. (.+?)(?: · |$)", block, re.M)
    return {"title": m.group(1), "company": m.group(2),
            "gap": gap.group(1) if gap else "", "gap_answer": gap.group(2) if gap else "",
            "lane": lane.group(2).strip() if lane else ""}


def job_questions(job: dict | None) -> list[str]:
    if not job:
        return []
    qs = [
        f"Why do you want the {job['title']} role at {job['company']}?",
        f"What would you do in your first month as {job['title']}?",
        f"Which part of your background fits a {job['lane']} role best, and why?",
    ]
    if job["gap"] and job["gap"] != "Nothing explicit in the posting":
        qs.append(f"This role needs {job['gap']}. What's your experience there?")
    return qs


def question_deck(job: dict | None) -> list[str]:
    """Interleave job-specific and generic questions so every session mixes both."""
    js, gs = job_questions(job), GENERIC[:]
    random.shuffle(gs)
    deck = []
    while js or gs:
        if js:
            deck.append(js.pop(0))
        if gs:
            deck.append(gs.pop(0))
    return deck


# ---------------------------------------------------------------- audio


def say(text: str, rate: int | None = None, quiet: bool = False) -> None:
    if quiet:
        return
    cmd = ["say"] + (["-r", str(rate)] if rate else []) + [text]
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        pass


def record() -> Path:
    import numpy as np
    import sounddevice as sd
    import wave

    input("  [Enter] to start recording… ")
    q: queue.Queue = queue.Queue()
    stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=lambda d, *_: q.put(d.copy()))
    with stream:
        print("  ● recording — [Enter] to stop")
        input()
    chunks = []
    while not q.empty():
        chunks.append(q.get())
    audio = np.concatenate(chunks) if chunks else np.zeros((1, 1), dtype="int16")
    path = Path(tempfile.mkstemp(suffix=".wav")[1])
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())
    print(f"  {len(audio) / SAMPLE_RATE:.1f}s captured")
    return path


_model = None


def load_audio(path: Path):
    """Decode any audio file to 16 kHz mono float32 with ffmpeg (avoids PyAV version breakage)."""
    import numpy as np

    raw = subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True, check=True,
    ).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def transcribe(path: Path, model_name: str) -> tuple[str, list[tuple[str, float]]]:
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        print(f"  loading faster-whisper '{model_name}' (int8, CPU)…")
        _model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _ = _model.transcribe(load_audio(path), language="en", word_timestamps=True, vad_filter=True)
    words = []
    for seg in segments:
        for w in seg.words or []:
            words.append((w.word.strip(), w.probability))
    text = " ".join(w for w, _ in words)
    return text, words


def flagged(words: list[tuple[str, float]]) -> list[tuple[str, float]]:
    out = []
    for w, p in words:
        clean = re.sub(r"[^\w'’-]", "", w)
        if clean and p < LOW_PROB:
            out.append((clean, p))
    return out


# ---------------------------------------------------------------- feedback


def claude_feedback(question: str, transcript: str) -> tuple[str, str] | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic

    profile = settings.read_optional(CANDIDATE.get("profile_file"))
    rules = settings.read_optional(CANDIDATE.get("rules_file"))
    if not profile:
        print("  (no profile_file in config.yaml: skipping Claude feedback)")
        return None
    persona = settings.get("coach.persona", "a candidate interviewing in English")
    prompt = f"""You are an English interview coach for {persona}.

RULES (must obey):
{rules}

PROFILE (the ONLY source of facts about the candidate):
{profile}

QUESTION: {question}
CANDIDATE'S SPOKEN ANSWER (speech-to-text transcript): {transcript}

Reply in exactly this format:
FEEDBACK:
- (max 5 bullets total, each about grammar, sentence structure or clarity; quote his words and give the fix)
IMPROVED:
(an improved answer under 90 words, first person, short spoken sentences, using only PROFILE facts,
no numeric results, no "passionate", "proven track record", "data-driven", "results-oriented")"""
    try:
        msg = anthropic.Anthropic().messages.create(
            model=os.environ.get("TALK_CLAUDE_MODEL", settings.get("llm.model", "claude-sonnet-5-5")), max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        print(f"  (Claude feedback failed: {e})")
        return None
    text = msg.content[0].text
    fb, _, improved = text.partition("IMPROVED:")
    fb = fb.replace("FEEDBACK:", "").strip()
    improved = improved.strip()
    problems = letter_ok(improved)
    if len(improved.split()) >= 90:
        problems.append(f"{len(improved.split())} words")
    if problems:
        improved += f"\n\n(⚠ rules check: {problems}, so don't memorize the flagged parts)"
    return fb, improved


# ---------------------------------------------------------------- log


def save_attempt(question: str, attempt: int, transcript: str, flags, feedback) -> Path:
    PRACTICE.mkdir(exist_ok=True)
    now = datetime.now()
    path = PRACTICE / f"{now:%Y-%m-%d}.md"
    if not path.exists():
        path.write_text(f"# Practice — {now:%Y-%m-%d}\n")
    lines = [f"\n## {now:%H:%M} · {question} (attempt {attempt})", "", f"**Transcript:** {transcript or '_(nothing heard)_'}", ""]
    lines.append("**Practice these:** " + (", ".join(f"{w} ({p:.2f})" for w, p in flags) if flags else "none"))
    if feedback:
        lines += ["", "**Feedback:**", feedback[0], "", "**Improved answer:**", feedback[1]]
    else:
        lines += ["", "_No Claude feedback (ANTHROPIC_API_KEY not set)._"]
    with path.open("a") as f:
        f.write("\n".join(lines) + "\n")
    with FLAGS_LOG.open("a") as f:
        for w, p in flags:
            f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "word": w.lower(), "p": round(p, 3)}) + "\n")
    return path


def review(days: int = 7, top: int = 20) -> None:
    since = datetime.now() - timedelta(days=days)
    counts, probs = Counter(), {}
    if FLAGS_LOG.exists():
        for line in FLAGS_LOG.read_text().splitlines():
            r = json.loads(line)
            if datetime.fromisoformat(r["ts"]) >= since and r["word"] not in STOPWORDS:
                counts[r["word"]] += 1
                probs.setdefault(r["word"], []).append(r["p"])
    if not counts:
        print(f"No flagged words in the last {days} days. Run `talk` first.")
        return
    print(f"Most-flagged words, last {days} days. Drill these:\n")
    for w, n in counts.most_common(top):
        print(f"  {w:<18} ×{n}   avg confidence {sum(probs[w]) / len(probs[w]):.2f}")


# ---------------------------------------------------------------- main


def main(argv=None):
    ap = argparse.ArgumentParser(prog="talk", description="Voice interview coach.")
    ap.add_argument("--review", action="store_true", help="most-flagged words of the week")
    ap.add_argument("--model", default=os.environ.get("TALK_WHISPER_MODEL", "small.en"), help="faster-whisper model")
    ap.add_argument("--file", help="transcribe this audio file instead of recording (testing)")
    ap.add_argument("--question", help="use this question instead of the deck")
    ap.add_argument("--quiet", action="store_true", help="don't speak with `say`")
    ap.add_argument("--once", action="store_true", help="one attempt, then exit")
    args = ap.parse_args(argv)

    if args.review:
        return review()

    job = top_job()
    if job:
        print(f"Today's #1 job: {job['title']} — {job['company']}  (gap: {job['gap'] or 'n/a'})")
    deck = [args.question] if args.question else question_deck(job)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set: transcript + flagged words only, no Claude feedback.")

    qi, attempt = 0, 1
    while True:
        q = deck[qi % len(deck)]
        print(f"\n━━ Q{qi + 1}: {q}")
        say(q, quiet=args.quiet)
        audio = Path(args.file) if args.file else record()
        print("  transcribing…")
        transcript, words = transcribe(audio, args.model)
        if not args.file:
            audio.unlink(missing_ok=True)
        flags = flagged(words)
        print(f"\n  You said: {transcript or '(nothing heard)'}")
        print("  Practice these: " + (", ".join(f"{w} ({p:.2f})" for w, p in flags) if flags else "none"))
        fb = claude_feedback(q, transcript) if transcript else None
        if fb:
            print(f"\n  Feedback:\n{fb[0]}\n\n  Improved answer:\n  {fb[1]}")
            say(fb[1].split("\n\n(⚠")[0], rate=155, quiet=args.quiet)
        log = save_attempt(q, attempt, transcript, flags, fb)
        print(f"  saved → {log.relative_to(ROOT)}")
        if args.once:
            return
        choice = input("\n  [r] retry · [n] next · [q] quit > ").strip().lower()
        if choice == "q":
            return
        if choice == "r":
            attempt += 1
        else:
            qi, attempt = qi + 1, 1


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nbye")
