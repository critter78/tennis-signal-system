#!/usr/bin/env python3
"""
Tennis P&L Dashboard Generator

Reads picks.jsonl, injects data into dashboard.html template,
and outputs a ready-to-use HTML dashboard.

Usage:
    python3 07_dashboard.py --open
"""

import json
import sys
import os
import subprocess
import argparse
from pathlib import Path


def load_picks_data(picks_jsonl_path):
    """Load all picks from JSONL file."""
    picks = []
    try:
        with open(picks_jsonl_path, 'r') as f:
            for line in f:
                if line.strip():
                    try:
                        pick = json.loads(line)
                        picks.append(pick)
                    except json.JSONDecodeError:
                        continue
    except FileNotFoundError:
        print(f"Warning: {picks_jsonl_path} not found. Dashboard will have empty data.")
    return picks


def inject_data_into_template(template_path, picks_data, output_path):
    """Read template, inject picks data, and save to output."""
    try:
        with open(template_path, 'r') as f:
            content = f.read()
    except FileNotFoundError:
        print(f"Error: Template not found at {template_path}")
        sys.exit(1)

    # Serialize picks data to JSON (safe for embedding in JavaScript)
    picks_json = json.dumps(picks_data, indent=2)

    # Replace the placeholder with actual data (handle with/without semicolon)
    replacement = f"window.PICKS_DATA = {picks_json};"

    if "window.PICKS_DATA = [];" in content:
        content = content.replace("window.PICKS_DATA = [];", replacement)
    elif "window.PICKS_DATA = []" in content:
        content = content.replace("window.PICKS_DATA = []", replacement)
    else:
        print("Warning: Placeholder 'window.PICKS_DATA = []' not found in template.")
        if "window.PICKS_DATA" in content:
            print("Found window.PICKS_DATA, attempting regex replace...")
            import re
            content = re.sub(r'window\.PICKS_DATA\s*=\s*\[.*?\];?', replacement, content)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Write output
    with open(output_path, 'w') as f:
        f.write(content)

    print(f"Dashboard created: {output_path}")
    return output_path


def open_in_chrome(file_path):
    """Open the generated HTML file in Chrome."""
    file_url = f"file://{os.path.abspath(file_path)}"

    try:
        # Try macOS
        subprocess.Popen(["open", "-a", "Google Chrome", file_url])
        print(f"Opened in Chrome: {file_url}")
    except FileNotFoundError:
        try:
            # Try Windows
            subprocess.Popen(["start", "chrome", file_url], shell=True)
            print(f"Opened in Chrome: {file_url}")
        except Exception as e:
            try:
                # Try Linux
                subprocess.Popen(["google-chrome", file_url])
                print(f"Opened in Chrome: {file_url}")
            except FileNotFoundError:
                print(f"Could not open Chrome. Open manually: {file_url}")


def main():
    parser = argparse.ArgumentParser(
        description="Generate Tennis P&L Dashboard from picks data"
    )
    parser.add_argument(
        "--open", action="store_true", help="Open the dashboard in Chrome after generation"
    )
    parser.add_argument(
        "--template",
        default="dashboard.html",
        help="Path to dashboard template (default: dashboard.html in current dir)",
    )
    parser.add_argument(
        "--picks",
        default="logs/picks.jsonl",
        help="Path to picks.jsonl (default: logs/picks.jsonl)",
    )
    parser.add_argument(
        "--output",
        default="cards/dashboard.html",
        help="Output path (default: cards/dashboard.html)",
    )

    args = parser.parse_args()

    # Convert to absolute paths
    template_path = os.path.abspath(args.template)
    picks_path = os.path.abspath(args.picks)
    output_path = os.path.abspath(args.output)

    print("=" * 60)
    print("Tennis P&L Dashboard Generator")
    print("=" * 60)

    # Load picks data
    print(f"Loading picks from: {picks_path}")
    picks_data = load_picks_data(picks_path)
    print(f"Loaded {len(picks_data)} picks")

    # Inject into template and generate output
    print(f"Using template: {template_path}")
    inject_data_into_template(template_path, picks_data, output_path)

    # Open in Chrome if requested
    if args.open:
        open_in_chrome(output_path)

    print("=" * 60)
    print("Done!")


if __name__ == "__main__":
    main()
