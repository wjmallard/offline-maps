"""Pack a deck directory into a single tradeable .deck file.

A .deck is an ordinary zip of the deck directory layout — points.parquet plus
the optional meta.parquet, deck.yaml, photos/, thumbs/ — stored rather than
compressed, since parquet and JPEG already are; that keeps packing fast and
lets the viewer serve members straight out of the archive. Any zip tool can
inspect the result.

--photos picks the size tier: none (points + metadata only), thumbs
(popup-sized images, the default), or full (adds the full-resolution photos/).
Working files (dotfiles, *.tmp, sync checkpoints) never make it in.
"""

import argparse
import os
import zipfile
from pathlib import Path

from tqdm import tqdm

_TOP_LEVEL = (
    "points.parquet",
    "meta.parquet",
    "deck.yaml",
)


def deck_members(deck_dir, photos):
    """Yield (path, member_name) for everything the .deck should contain."""
    members = []
    for name in _TOP_LEVEL:
        path = deck_dir / name
        if path.is_file():
            members.append((path, name))
    subdirs = []
    if photos in ("thumbs", "full"):
        subdirs.append("thumbs")
    if photos == "full":
        subdirs.append("photos")
    for subdir in subdirs:
        directory = deck_dir / subdir
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file() or path.name.startswith(".") or path.suffix == ".tmp":
                continue
            members.append((path, f"{subdir}/{path.name}"))
    return members


def pack(deck_dir, output_path, photos):
    """Write the .deck zip atomically; return (member_count, byte_size)."""
    members = deck_members(deck_dir, photos)
    tmp_path = output_path.parent / (output_path.name + ".tmp")
    with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_STORED) as archive:
        for path, name in tqdm(members, desc="packing", unit="file"):
            archive.write(path, name)
    os.replace(tmp_path, output_path)
    return len(members), output_path.stat().st_size


def main():
    parser = argparse.ArgumentParser(
        description="Pack a deck directory into a single tradeable .deck file.",
    )
    parser.add_argument(
        "deck_dir",
        type=Path,
        help="deck directory to pack (must contain points.parquet)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="destination .deck file (default: <deck name>.deck in the current directory)",
    )
    parser.add_argument(
        "--photos",
        choices=["none", "thumbs", "full"],
        default="thumbs",
        help="image tier to bundle (default: thumbs — popup-sized only)",
    )
    args = parser.parse_args()

    if not (args.deck_dir / "points.parquet").is_file():
        parser.error(f"{args.deck_dir} has no points.parquet — not a deck directory.")

    output_path = args.output or Path(f"{args.deck_dir.resolve().name}.deck")
    count, size = pack(args.deck_dir, output_path, args.photos)
    print(f"Wrote {output_path} — {count:,} files, {size / 1_000_000:,.1f} MB (photos: {args.photos})")


if __name__ == "__main__":
    main()
