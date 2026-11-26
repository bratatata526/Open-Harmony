import sys
mods = ["serial","openai","yaml","jinja2","rich"]
ok, miss = [], []
for m in mods:
    try:
        __import__(m); ok.append(m)
    except ImportError:
        miss.append(m)
parts = [f"{m}:ok" for m in ok] + [f"{m}:MISSING" for m in miss]
print(" ".join(parts))
sys.exit(1 if miss else 0)
