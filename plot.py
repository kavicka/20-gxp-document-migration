"""results.json → assets/*.png (the step's visual example: where every legacy document ended up).

reconciliation.png  294 source documents → loaded / quarantined per issue, lift-and-shift vs validated pipeline
accuracy.png        field-level metadata accuracy on the 249 clean documents
"""

import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

R = json.loads(Path("results.json").read_text())
Path("assets").mkdir(exist_ok=True)
BLUE, RED, GREEN, GRAY, ORANGE, DARK = "#2563eb", "#dc2626", "#16a34a", "#94a3b8", "#ea580c", "#1e293b"


def save(fig, name: str) -> None:
    fig.tight_layout()
    fig.savefig(f"assets/{name}", dpi=150)
    plt.close(fig)


def reconciliation() -> None:
    src, b, p = R["source"], R["baseline"], R["pipeline"]
    fig, ax = plt.subplots(figsize=(10, 3.4))
    rows = [
        ("lift-and-shift", [(b["loaded"] - b["defective_loaded"], GREEN, "clean, loaded"),
                            (b["defective_loaded"], RED, "defective, loaded"),
                            (src["documents"] - b["loaded"], GRAY, "failed (file missing)")]),
        ("validated pipeline", [(p["loaded"], GREEN, "clean, loaded"),
                                (p["defective_loaded"], RED, "defective, loaded"),
                                (p["quarantined"], ORANGE, "quarantined with a reason")]),
    ]  # fmt: skip
    for y, (_label, parts) in enumerate(rows):
        left = 0
        for n, color, name in parts:
            if n:
                ax.barh(y, n, left=left, color=color, label=name)
                ax.text(left + n / 2, y, str(n), ha="center", va="center", color="white", fontsize=9)
            left += n
    ax.set(yticks=[0, 1], yticklabels=[r[0] for r in rows], xlabel="documents")
    ax.invert_yaxis()
    ax.set_title(f"{src['documents']} legacy documents, {src['injected_defects']} with an injected defect", fontsize=10)
    handles, labels = ax.get_legend_handles_labels()
    ax.legend(dict(zip(labels, handles, strict=True)).values(), dict(zip(labels, handles, strict=True)).keys(),
              fontsize=8, loc="lower center", bbox_to_anchor=(0.5, -0.42), ncol=4)  # fmt: skip
    save(fig, "reconciliation.png")


def accuracy() -> None:
    fields = list(R["pipeline"]["accuracy"]["per_field"])
    x = np.arange(len(fields))
    fig, ax = plt.subplots(figsize=(10, 3.6))
    for j, (k, color) in enumerate((("baseline", GRAY), ("pipeline", BLUE))):
        vals = [R[k]["accuracy"]["per_field"][f] for f in fields]
        bars = ax.bar(x + (j - 0.5) * 0.38, vals, 0.38, color=color,
                      label="lift-and-shift" if k == "baseline" else "validated pipeline")  # fmt: skip
        ax.bar_label(bars, fmt="%.2f", fontsize=7)
    ax.set(xticks=x, xticklabels=[f.replace("_", "\n") for f in fields], ylim=(0, 1.12), ylabel="share correct")
    ax.set_title(f"Metadata accuracy on the {R['pipeline']['accuracy']['n']} clean documents", fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, axis="y")
    save(fig, "accuracy.png")


if __name__ == "__main__":
    reconciliation()
    accuracy()
