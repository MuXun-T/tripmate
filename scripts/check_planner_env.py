"""T0.2 acceptance: sandbox tools import, per-city row counts, split sizes (counts only)."""

import subprocess

from chinatravel.environment.language import city_names
from chinatravel.environment.tools import Accommodations, Attractions, IntercityTransport, Restaurants, Transportation

from tripmate_planner.paths import ROOT

_ = (IntercityTransport, Transportation)
att, res, acc = Attractions(), Restaurants(), Accommodations()
totals = {"attractions": 0, "restaurants": 0, "accommodations": 0}
for city in city_names("zh"):
    n_a = len(att.select(city, "name", lambda x: True))
    n_r = len(res.select(city, "name", lambda x: True))
    n_h = len(acc.select(city, "name", lambda x: True))
    totals["attractions"] += n_a
    totals["restaurants"] += n_r
    totals["accommodations"] += n_h
    print(f"{city}: attractions={n_a} restaurants={n_r} accommodations={n_h}")
print("totals:", totals, "paper: 3413/4655/4124")

from datasets import load_dataset  # noqa: E402

import json  # noqa: E402

q = json.loads((ROOT / "configs/planner/versions.json").read_text())["query_dataset"]
ds = load_dataset(q["repo"], name="default", revision=q["revision"])
print({s: len(ds[s]) for s in ("easy", "medium", "human")})
print(subprocess.run(["du", "-sh", ".venv-planner", "third_party", ".cache"], cwd=ROOT,
                     capture_output=True, text=True).stdout)
