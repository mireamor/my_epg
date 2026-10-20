
import gzip
import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path

import requests
from lxml import etree

EPG_SOURCES = [
    "https://iptvx.one/EPG",
    "http://cdn.epg.one/pp.xml.gz",
]
ICONS_SOURCE = (
    "https://raw.githubusercontent.com/Lorax121/epg_v2/"
    "main/data/epg2.xml.gz"
)

WORK = Path("work")
PUBLIC = Path("public")
WORK.mkdir(exist_ok=True)
PUBLIC.mkdir(exist_ok=True)

DB_PATH = WORK / "epg.sqlite"
OUTPUT = PUBLIC / "epg.xml.gz"


def download(url, path):
    print("Downloading:", url)
    with requests.get(
        url,
        stream=True,
        timeout=(30, 300),
        headers={"User-Agent": "my-epg-builder/1.0"},
    ) as response:
        response.raise_for_status()
        with open(path, "wb") as f:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    f.write(chunk)


def open_xml(path):
    raw = open(path, "rb")
    magic = raw.read(2)
    raw.seek(0)
    if magic == b"\x1f\x8b":
        return gzip.GzipFile(fileobj=raw)
    return raw


def parse_xml(path, on_channel, on_programme):
    with open_xml(path) as stream:
        context = etree.iterparse(
            stream,
            events=("end",),
            tag=("channel", "programme"),
            recover=True,
            huge_tree=True,
            no_network=True,
        )
        for _, elem in context:
            if elem.tag == "channel":
                on_channel(elem)
            elif elem.tag == "programme":
                on_programme(elem)

            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]


def icon_from_channel(elem):
    icons = elem.findall("icon")
    return icons[0].get("src") if icons else None


def main():
    db = sqlite3.connect(DB_PATH)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id TEXT PRIMARY KEY,
            xml BLOB NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS programmes (
            key TEXT PRIMARY KEY,
            start TEXT NOT NULL,
            xml BLOB NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS icons (
            id TEXT PRIMARY KEY,
            src TEXT NOT NULL
        )
    """)
    db.execute("DELETE FROM channels")
    db.execute("DELETE FROM programmes")
    db.execute("DELETE FROM icons")
    db.commit()

    # First, read Lorax121's channel IDs and icon URLs.
    icon_file = WORK / "lorax.xml.gz"
    download(ICONS_SOURCE, icon_file)

    def save_lorax_channel(elem):
        channel_id = elem.get("id")
        src = icon_from_channel(elem)
        if channel_id and src:
            db.execute(
                "INSERT OR REPLACE INTO icons VALUES (?, ?)",
                (channel_id, src),
            )

    parse_xml(icon_file, save_lorax_channel, lambda elem: None)
    db.commit()

    # Download and combine schedules from both EPG sources.
    for index, url in enumerate(EPG_SOURCES, start=1):
        source_file = WORK / f"source_{index}.xml"

        download(url, source_file)

        def save_channel(elem):
            channel_id = elem.get("id")
            if not channel_id:
                return
            db.execute(
                "INSERT OR IGNORE INTO channels VALUES (?, ?)",
                (
                    channel_id,
                    sqlite3.Binary(
                        etree.tostring(elem, encoding="utf-8")
                    ),
                ),
            )

        def save_programme(elem):
            channel_id = elem.get("channel")
            start = elem.get("start", "")
            stop = elem.get("stop", "")
            title = " ".join(
                (elem.findtext("title") or "").split()
            )
            if not channel_id or not start or not title:
                return

            # Remove exact schedule duplicates across sources.
            signature = "\x1f".join(
                (channel_id, start, stop, title.casefold())
            )
            key = hashlib.sha256(
                signature.encode("utf-8")
            ).hexdigest()

            db.execute(
                "INSERT OR IGNORE INTO programmes VALUES (?, ?, ?)",
                (
                    key,
                    start,
                    sqlite3.Binary(
                        etree.tostring(elem, encoding="utf-8")
                    ),
                ),
            )

        parse_xml(source_file, save_channel, save_programme)
        db.commit()
        print("Processed source", index)

    # Replace channel icons when Lorax has the same channel ID.
    with gzip.open(OUTPUT, "wb", compresslevel=6) as output:
        with etree.xmlfile(output, encoding="utf-8") as writer:
            writer.write_declaration()
            with writer.element("tv", attrib={
                "generator-info-name": "mireamor/my_epg"
            }):
                cursor = db.execute(
                    "SELECT id, xml FROM channels ORDER BY id"
                )
                for channel_id, xml in cursor:
                    elem = etree.fromstring(xml)
                    row = db.execute(
                        "SELECT src FROM icons WHERE id = ?",
                        (channel_id,),
                    ).fetchone()

                    if row:
                        for old_icon in elem.findall("icon"):
                            elem.remove(old_icon)
                        elem.append(etree.Element(
                            "icon", src=row[0]
                        ))

                    writer.write(elem)

                cursor = db.execute("""
                    SELECT xml FROM programmes
                    ORDER BY start, key
                """)
                for (xml,) in cursor:
                    writer.write(etree.fromstring(xml))

    db.close()
    print("Created:", OUTPUT)
    print("Size:", round(OUTPUT.stat().st_size / 1024 / 1024, 1), "MB")


if __name__ == "__main__":
    main()
