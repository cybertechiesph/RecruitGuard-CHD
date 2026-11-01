"""Keep the Filipino translation of the applicant portal up to date, without GNU gettext.

Django's makemessages / compilemessages need the GNU gettext tools, which are often missing
(Windows laptops, slim servers). This command does the same two jobs in pure Python:

1. Extract every translatable string: {% translate %} / {% blocktranslate %} in templates
   (read with Django's own template-to-gettext converter, so msgids match exactly what
   Django looks up) and gettext()/gettext_lazy()/ngettext() calls in Python files.
2. Merge them into locale/fil/LC_MESSAGES/django.po (existing translations are kept; new
   strings get an empty translation) and compile django.mo, which Django reads at runtime.

    python manage.py update_translations          # refresh the .po and compile the .mo
    python manage.py update_translations --check  # fail if any string has no translation

To translate: fill in the empty msgstr "" entries in django.po, then run the command again.
The compiled django.mo is committed, so the server needs no extra tools.
"""

import ast
import re
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.translation.template import templatize

LANGUAGE = "fil"
GETTEXT_FUNCTIONS = {
    "_": "single",
    "gettext": "single",
    "gettext_lazy": "single",
    "gettext_noop": "single",
    "ngettext": "plural",
    "ngettext_lazy": "plural",
    "pgettext": "context",
    "pgettext_lazy": "context",
}
PO_HEADER = (
    "Project-Id-Version: DOH-CHD CALABARZON Recruitment Portal\n"
    "Language: fil\n"
    "MIME-Version: 1.0\n"
    "Content-Type: text/plain; charset=UTF-8\n"
    "Content-Transfer-Encoding: 8bit\n"
    "Plural-Forms: nplurals=2; plural=(n != 1);\n"
)


# ── extraction ──────────────────────────────────────────────────────────────
_CALL_RE = re.compile(r"\b(" + "|".join(sorted(GETTEXT_FUNCTIONS, key=len, reverse=True)) + r")\(")


def _read_literal(source, index):
    """Read one Python string literal (optionally u/r-prefixed) starting at index.
    Returns (value, next_index) or (None, index)."""
    start = index
    while index < len(source) and source[index] in "uUrRbB":
        index += 1
    if index >= len(source) or source[index] not in "'\"":
        return None, start
    quote = source[index]
    index += 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return ast.literal_eval(source[start:index + 1]), index + 1
        index += 1
    return None, start


def _calls_in_tokens(source):
    """(kind, [string args]) for gettext-style calls in templatize() output."""
    for match in _CALL_RE.finditer(source):
        kind = GETTEXT_FUNCTIONS[match.group(1)]
        index, args = match.end(), []
        while True:
            while index < len(source) and source[index].isspace():
                index += 1
            value, index = _read_literal(source, index)
            if value is None:
                break
            literal = value
            while True:  # implicit concatenation
                probe = index
                while probe < len(source) and source[probe].isspace():
                    probe += 1
                more, after = _read_literal(source, probe)
                if more is None:
                    break
                literal += more
                index = after
            args.append(literal)
            while index < len(source) and source[index].isspace():
                index += 1
            if index < len(source) and source[index] == ",":
                index += 1
                continue
            break
        if args:
            yield kind, args


def _entry_from_call(kind, args):
    if kind == "single":
        return (None, args[0], None)
    if kind == "plural" and len(args) >= 2:
        return (None, args[0], args[1])
    if kind == "context" and len(args) >= 2:
        return (args[0], args[1], None)
    return None


def extract_entries(base_dir):
    """Ordered {(context, msgid, msgid_plural): [source files]}."""
    found = {}

    def add(key, origin):
        if key and key[1]:
            found.setdefault(key, [])
            if origin not in found[key]:
                found[key].append(origin)

    template_roots = [base_dir / "templates", base_dir / "recruitment" / "templates"]
    for root in template_roots:
        for path in sorted(root.rglob("*.html")):
            source = path.read_text(encoding="utf-8")
            if "{% load i18n" not in source and "{% load static i18n" not in source:
                continue
            origin = path.relative_to(base_dir).as_posix()
            for kind, args in _calls_in_tokens(templatize(source, origin=origin)):
                add(_entry_from_call(kind, args), origin)

    for folder in (base_dir / "recruitment", base_dir / "config"):
        for path in sorted(folder.rglob("*.py")):
            if "migrations" in path.parts or path.name.startswith("test"):
                continue
            source = path.read_text(encoding="utf-8")
            if "gettext" not in source:
                continue
            origin = path.relative_to(base_dir).as_posix()
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                kind = GETTEXT_FUNCTIONS.get(name)
                args = [arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
                if kind and args:
                    add(_entry_from_call(kind, args), origin)
    return found


# ── .po read / write ────────────────────────────────────────────────────────
def _po_unquote(line):
    return ast.literal_eval(line.strip())


def read_po(path):
    """{(context, msgid, msgid_plural): [msgstr, ...]} from a .po file."""
    entries = {}
    if not path.exists():
        return entries
    current, field = {}, None

    def flush():
        if "msgid" in current and current["msgid"]:
            key = (current.get("msgctxt"), current["msgid"], current.get("msgid_plural"))
            if key[2] is None:
                entries[key] = [current.get("msgstr", "")]
            else:
                entries[key] = [current.get(f"msgstr[{i}]", "") for i in range(2)]

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            if not line and current:
                flush()
                current, field = {}, None
            continue
        if line.startswith('"'):
            current[field] = current.get(field, "") + _po_unquote(line)
            continue
        field, _, rest = line.partition(" ")
        if field == "msgctxt" and current.get("msgid") is not None:
            flush()
            current = {}
        current[field] = _po_unquote(rest)
    flush()
    return entries


def _po_quote(text):
    escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t")
    if "\n" not in escaped:
        return f'"{escaped}"'
    pieces = escaped.split("\n")
    lines = [f'"{piece}\\n"' for piece in pieces[:-1]]
    if pieces[-1]:
        lines.append(f'"{pieces[-1]}"')
    return '""\n' + "\n".join(lines)


def write_po(path, found, existing):
    out = ["# Filipino translation of the DOH-CHD CALABARZON applicant portal.",
           "# Maintained with: python manage.py update_translations",
           'msgid ""', "msgstr " + _po_quote(PO_HEADER), ""]
    for key, origins in found.items():
        context, msgid, plural = key
        translations = existing.get(key) or ([""] if plural is None else ["", ""])
        out.append("#: " + " ".join(origins))
        if context is not None:
            out.append("msgctxt " + _po_quote(context))
        out.append("msgid " + _po_quote(msgid))
        if plural is None:
            out.append("msgstr " + _po_quote(translations[0]))
        else:
            out.append("msgid_plural " + _po_quote(plural))
            for index, text in enumerate(translations):
                out.append(f"msgstr[{index}] " + _po_quote(text))
        out.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


# ── .mo compile (GNU gettext binary format) ─────────────────────────────────
def write_mo(path, entries):
    catalog = {"": PO_HEADER}
    for (context, msgid, plural), translations in entries.items():
        if not all(translations):
            continue  # untranslated: Django falls back to the English text
        key = msgid if plural is None else f"{msgid}\x00{plural}"
        if context is not None:
            key = f"{context}\x04{key}"
        catalog[key] = "\x00".join(translations)
    keys = sorted(catalog)
    ids = [key.encode("utf-8") for key in keys]
    strs = [catalog[key].encode("utf-8") for key in keys]
    count = len(keys)
    header_size = 7 * 4
    ids_table = header_size
    strs_table = ids_table + count * 8
    data_start = strs_table + count * 8
    id_offsets, str_offsets, blob = [], [], b""
    for value in ids:
        id_offsets.append((len(value), data_start + len(blob)))
        blob += value + b"\x00"
    for value in strs:
        str_offsets.append((len(value), data_start + len(blob)))
        blob += value + b"\x00"
    output = struct.pack("<7I", 0x950412DE, 0, count, ids_table, strs_table, 0, 0)
    for length, offset in id_offsets:
        output += struct.pack("<2I", length, offset)
    for length, offset in str_offsets:
        output += struct.pack("<2I", length, offset)
    path.write_bytes(output + blob)


class Command(BaseCommand):
    help = "Extract applicant-portal strings, update the Filipino .po and compile the .mo."

    def add_arguments(self, parser):
        parser.add_argument("--check", action="store_true", help="Fail if any string is untranslated.")

    def handle(self, *args, **options):
        base_dir = Path(settings.BASE_DIR)
        po_path = base_dir / "locale" / LANGUAGE / "LC_MESSAGES" / "django.po"
        found = extract_entries(base_dir)
        existing = read_po(po_path)
        merged = {key: existing.get(key) or ([""] if key[2] is None else ["", ""]) for key in found}
        missing = [key[1] for key, translations in merged.items() if not all(translations)]
        if options["check"]:
            if missing:
                raise CommandError(
                    f"{len(missing)} applicant-portal string(s) have no Filipino translation, e.g. {missing[0]!r}. "
                    "Run update_translations and fill them in locale/fil/LC_MESSAGES/django.po."
                )
            self.stdout.write(self.style.SUCCESS(f"All {len(merged)} strings are translated."))
            return
        write_po(po_path, found, existing)
        write_mo(po_path.with_suffix(".mo"), merged)
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(merged)} strings; {len(merged) - len(missing)} translated, {len(missing)} missing."
            )
        )
