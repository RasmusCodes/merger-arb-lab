import pandas as pd

pd.set_option("display.width", 250)
pd.set_option("display.max_colwidth", 60)
ev = pd.read_csv("data/derived/antitrust/events.csv")
print(ev[["deal", "event_date", "timing", "event_type", "jurisdiction", "direction", "confidence"]].to_string())
print(ev.columns.tolist())
