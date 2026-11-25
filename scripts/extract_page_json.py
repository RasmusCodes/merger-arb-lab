"""Convert a browser-pane get_page_text spill file (JSON array of {type,text}) into a plain UTF-8 text file."""
import json, sys, pathlib

src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
items = json.loads(src.read_text(encoding="utf-8"))
text = "\n".join(it.get("text", "") for it in items if isinstance(it, dict))
dst.parent.mkdir(parents=True, exist_ok=True)
dst.write_text(text, encoding="utf-8")
print(dst, len(text), "chars", text.count("\n"), "lines")
