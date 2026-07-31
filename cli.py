#!/usr/bin/env python3
"""
ratman4080 CLI runner for Robin's dark-web OSINT pipeline.

The Streamlit UI was hanging mid-render (image push blocking the websocket
stream under WSL/drvfs). This CLI uses the exact same backend — refine,
search over Tor, filter, scrape, summarize — with the ratman preamble
already injected into every LLM call. No UI, no hang, same voice.

Usage:
    .venv/bin/python cli.py "stolen credit card dumps cvv shops"
    .venv/bin/python cli.py --preset ransomware_malware "lockbit affiliate handles"
    .venv/bin/python cli.py --interactive
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Make sure we import robin's modules from this directory regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import (
    get_llm, refine_query, filter_results, generate_summary, suggest_pivots,
)
from search import get_search_results
from scrape import scrape_multiple
from config import CUSTOM_API_MODEL


RAT = "\033[35m"      # magenta — the rat
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"

PRESETS = {
    "threat_intel":        "Dark web threat intel (default)",
    "ransomware_malware":  "Ransomware / malware focus",
    "personal_identity":   "Personal / identity exposure",
    "corporate_espionage": "Corporate espionage / data leaks",
}


def squeak(msg: str, color: str = RAT) -> None:
    print(f"{color}ratman4080:{RESET} {msg}", flush=True)


def step(msg: str) -> None:
    print(f"{DIM}  → {msg}{RESET}", flush=True)


def run_pipeline(query: str, model: str, preset: str,
                 threads: int, max_results: int, max_scrape: int) -> None:
    squeak(f"whiskers forward. crumb: {BOLD}{query}{RESET}")
    step(f"model: {model} | preset: {preset}")

    t0 = time.time()
    squeak("gnawing the query into search-engine shape…")
    llm = get_llm(model)
    refined = refine_query(llm, query).strip()
    squeak(f"refined → {BOLD}{refined}{RESET}", GREEN)

    squeak("scurrying through Tor — hitting dark web search engines…")
    raw_results = get_search_results(refined.replace(" ", "+"), max_workers=threads)
    squeak(f"pulled {len(raw_results)} raw results off the wire", GREEN)
    if not raw_results:
        squeak("crumb's dry — Tor returned nothing. check the circuit.", YELLOW)
        return

    squeak("filtering the noise…")
    filtered = filter_results(llm, refined, raw_results)
    filtered = filtered[:max_results]
    squeak(f"{len(filtered)} results worth gnawing on", GREEN)

    squeak("scraping the .onion pages (slow — hidden services are slow)…")
    scraped = scrape_multiple(filtered[:max_scrape], max_workers=threads)
    squeak(f"scraped {sum(1 for s in scraped if s)} pages", GREEN)

    squeak("synthesizing — ratman voice on the wire…")
    summary = generate_summary(llm, query, scraped, preset=preset)

    squeak(f"pivots for the next run…", YELLOW)
    pivots = suggest_pivots(llm, query, scraped, preset=preset)

    dt = time.time() - t0
    print()
    print(f"{BOLD}{'=' * 72}{RESET}")
    print(f"{BOLD}{RAT} INVESTIGATION REPORT {RESET}{DIM}({dt:.1f}s){RESET}")
    print(f"{BOLD}{'=' * 72}{RESET}")
    print(summary)
    print(f"{BOLD}{'=' * 72}{RESET}")

    if pivots:
        print()
        squeak("suggested pivots — drop one back in to chase:")
        for i, p in enumerate(pivots, 1):
            print(f"  {YELLOW}{i}.{RESET} {p}")

    squeak("stashed. what's next.", GREEN)


def interactive(model: str, preset: str, threads: int,
                max_results: int, max_scrape: int) -> None:
    squeak("nest is open. drop crumbs. Ctrl-C to close the wire.")
    while True:
        try:
            q = input(f"\n{RAT}query>{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            squeak("wire closed. nest stays warm.")
            return
        if not q:
            continue
        if q.lower() in ("preset", "p"):
            print(f"{DIM}available:{RESET}")
            for k, v in PRESETS.items():
                print(f"  {k:24s} {v}")
            new = input(f"{RAT}preset>{RESET} ").strip() or preset
            if new in PRESETS:
                preset = new
                squeak(f"preset → {preset}", GREEN)
            else:
                squeak(f"unknown preset, staying on {preset}", YELLOW)
            continue
        try:
            run_pipeline(q, model, preset, threads, max_results, max_scrape)
        except KeyboardInterrupt:
            squeak("abort. next crumb.", YELLOW)
        except Exception as e:
            squeak(f"gnaw broke: {type(e).__name__}: {e}", YELLOW)


def main() -> int:
    p = argparse.ArgumentParser(description="Robin dark-web OSINT — ratman CLI")
    p.add_argument("query", nargs="?", help="search query (omit for --interactive)")
    p.add_argument("--interactive", "-i", action="store_true",
                   help="REPL loop — drop crumbs one at a time")
    p.add_argument("--model", default=CUSTOM_API_MODEL,
                   help=f"LLM model id (default: {CUSTOM_API_MODEL})")
    p.add_argument("--preset", default="threat_intel", choices=list(PRESETS))
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--max-results", type=int, default=50)
    p.add_argument("--max-scrape", type=int, default=10)
    args = p.parse_args()

    if not args.model:
        squeak("no model configured. set CUSTOM_API_MODEL in .env", YELLOW)
        return 2

    if args.interactive or not args.query:
        interactive(args.model, args.preset, args.threads,
                    args.max_results, args.max_scrape)
        return 0

    run_pipeline(args.query, args.model, args.preset,
                 args.threads, args.max_results, args.max_scrape)
    return 0


if __name__ == "__main__":
    sys.exit(main())
