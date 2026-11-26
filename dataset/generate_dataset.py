import csv
import random

OUTPUT_FILE = "sarathi_dataset.csv"
NUM_ROWS = 100
HEADERS = [
    "attendance",
    "assignment_completion",
    "study_hours",
    "consistency",
    "confidence",
    "final_score",
]


def generate_dataset(filename: str = OUTPUT_FILE, rows: int = NUM_ROWS):
    random.seed(42)  # Reproducible synthetic cohort data
    records = []

    for _ in range(rows):
        attendance = round(random.uniform(52.0, 100.0), 1)
        assignment_completion = round(
            min(100.0, max(40.0, attendance * 0.92 + random.uniform(-8.0, 10.0))), 1
        )
        study_hours = round(random.uniform(3.0, 22.0), 1)
        consistency = random.randint(2, 10)
        confidence = random.randint(2, 10)

        raw_score = (
            0.28 * attendance
            + 0.25 * assignment_completion
            + 1.10 * study_hours
            + 1.20 * consistency
            + 1.00 * confidence
            + random.uniform(-3.5, 3.5)
        )
        final_score = round(min(100.0, max(0.0, raw_score)), 1)

        records.append(
            [
                attendance,
                assignment_completion,
                study_hours,
                consistency,
                confidence,
                final_score,
            ]
        )

    with open(filename, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        writer.writerows(records)

    print(f"[SARATHI] Generated {rows} de-identified records in '{filename}'.")


if __name__ == "__main__":
    generate_dataset()
