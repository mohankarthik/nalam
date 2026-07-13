"""Export the codebook for human review: every analyte, what it means, who has data.

The codebook decides what the system can answer. If an analyte is missing or
wrong here, that question is silently unanswerable -- so it is worth one careful
human read.

Run:  python -m tools.export_analytes
"""

from __future__ import annotations

import os
from collections import defaultdict

from src import db
from src.normalize import load_codebook
from src.people import load_people

OUT = os.path.expanduser("~/nalam-analytes-review.md")

# Plain-English meaning. Where a value is derived or easily confused with a
# neighbour, the note says so -- those are the ones worth checking.
MEANING = {
    # Glucose
    "Fasting Blood Sugar": "Blood glucose after an overnight fast.",
    "PP Blood Sugar": "Blood glucose ~2h after a meal (post-prandial).",
    "HbA1c": "Average blood glucose over ~3 months. The headline diabetes number.",
    "Estimated Average Glucose": "Average glucose derived FROM HbA1c. Not measured.",
    # KFT
    "BUN (Urease/GLDH)": "Blood urea nitrogen. Kidney function.",
    "Urea": "Blood urea. Kidney function. Related to but not the same as BUN.",
    "Creatinine": "Kidney function. The main one. Range differs by sex.",
    "Uric Acid": "High levels cause gout. Range differs by sex.",
    "Calcium": "Serum calcium. Bone/parathyroid.",
    "Phosphorus": "Serum phosphate. Bone/kidney.",
    "Sodium": "Electrolyte.",
    "Potassium": "Electrolyte. Both high and low are dangerous.",
    "Chloride": "Electrolyte.",
    # LFT
    "SGOT": "Liver enzyme (AST). Also raised by muscle injury.",
    "SGPT": "Liver enzyme (ALT). More liver-specific than SGOT.",
    "Alkaline Phosphatase": "Liver/bile-duct and bone enzyme.",
    "Gamma Glutamyl Transpeptidase (GGT)": "Liver/bile enzyme; sensitive to alcohol.",
    "Total Protien": "Total serum protein. (Sheet spells it 'Protien'.)",
    "Albumin": "Main blood protein. Made by the liver. Serum, NOT urine albumin.",
    "Globulin": "Total protein minus albumin. Immune proteins.",
    "A:G Ratio": "Albumin-to-globulin ratio. Derived, not measured.",
    "Total Bilrubin": "Total bilirubin. Jaundice. (Sheet spells it 'Bilrubin'.)",
    "Direct Bilirubin": "Conjugated bilirubin. A SUBSET of total - not the same test.",
    # Lipid
    "Cholesterol": "Total cholesterol.",
    "HDL": "'Good' cholesterol. Higher is better. Range differs by sex.",
    "LDL": "'Bad' cholesterol. Lower is better.",
    "TGL": "Triglycerides.",
    "VLDL": "Very-low-density lipoprotein. Usually derived from triglycerides.",
    "Cholesterol:HDL Ratio": "Cardiac risk ratio. Derived, not measured.",
    # CBC
    "Haemoglobin": "Oxygen-carrying pigment. Anaemia if low.",
    "Packed cell volume": "Haematocrit - % of blood that is red cells.",
    "RBC": "Red cell COUNT (blood). Distinct from 'Urine RBC'.",
    "WBC": "White cell count (blood). Infection marker. Distinct from urine pus cells.",
    "Neutrophil": "Neutrophils as a PERCENTAGE of white cells.",
    "Lymphocyte": "Lymphocytes as a percentage.",
    "Eosinophil": "Eosinophils as a percentage. Raised in allergy/parasites.",
    "Basophil": "Basophils as a percentage.",
    "Monocyte": "Monocytes as a percentage.",
    "Absolute Neutrophil Count": "Neutrophils as a COUNT (cells/uL). Different test from the %.",
    "Absolute Lymphocyte Count": "Lymphocytes as a count. Different test from the %.",
    "Absolute Eosinophil Count": "Eosinophils as a count. Different test from the %.",
    "Absolute Monocyte Count": "Monocytes as a count. Different test from the %.",
    "Absolute Basophil Count": "Basophils as a count. Different test from the %.",
    "Platelet Count": "Clotting cells. NOTE: sheet's upper bound (4,100,000) looks 10x too high.",
    "MCV": "Mean red cell volume. Size of red cells.",
    "MCH": "Mean haemoglobin per red cell.",
    "MCHC": "Mean haemoglobin concentration per red cell.",
    "RDW-CV": "Variation in red cell size. Early anaemia marker.",
    "MPV": "Mean platelet volume.",
    "PDW": "Platelet size variation.",
    "Erythrocyte Sedimentation Rate": "ESR. Non-specific inflammation marker.",
    # Thyroid / hormones
    "T3": "Triiodothyronine. NOTE: labs switched ng/mL -> nmol/L in 2023; the sheet mixes both.",
    "T4": "Thyroxine.",
    "TSH": "Thyroid-stimulating hormone. The main thyroid screen.",
    "Prolactin": "Pituitary hormone.",
    # Iron / vitamins
    "Serum Ferritin": "Iron stores. Also rises with inflammation.",
    "Iron (TPTZ)": "Serum iron.",
    "Iron Binding Capacity (PSAP)": "TOTAL iron binding capacity (TIBC). NOT the same as UIBC.",
    "Vitamin B12": "B12. Low causes anaemia and neuropathy.",
    "Vitamin D": "25-hydroxy vitamin D. NOTE: labs report ng/mL or nmol/L - units matter.",
    # Infection markers
    "HBsAg": "Hepatitis B surface antigen. Positive = infection.",
    "VDRL": "Syphilis screen.",
    "PSA": "Prostate-specific antigen. Men only.",
    # Urine
    "Urine SG": "Urine specific gravity. Concentration.",
    "Urine pH": "Urine acidity.",
    "Urine Colour": "Visual colour.",
    "Urine Appearance": "Clear/turbid.",
    "Urine Volume": "Sample volume.",
    "Urine Protein": "Protein/albumin in URINE (dipstick). Kidney leak. NOT serum albumin.",
    "Urine Glucose": "Glucose in URINE. NOT blood glucose.",
    "Urine Ketone Bodies": "Ketones in urine.",
    "Urine Bile Salt": "Bile salts in urine.",
    "Urine Bile Pigments": "Bile pigments in urine.",
    "Urine Nitrite": "Suggests bacterial infection.",
    "Urine Leucocyte Esterase": "Suggests urinary infection.",
    "Urine Epithelial Cells": "Shed cells. Often contamination.",
    "Urine Pus Cells": "White cells in URINE. Infection. NOT the blood WBC count.",
    "Urine RBC": "Red cells in URINE. NOT the blood RBC count.",
    "Urine Casts": "Cylindrical structures. Kidney disease.",
    "Urine Crystals": "Crystals. Stones.",
    # Cardiac
    "Aorta": "Aortic root diameter (mm), 2D echo.",
    "LA": "Left atrium diameter (mm).",
    "LVIDD": "Left ventricle diameter, diastole (relaxed).",
    "LVIDS": "Left ventricle diameter, systole (contracted).",
    "IVSD": "Interventricular septum thickness, diastole.",
    "LVPWD": "Left ventricle posterior wall thickness, diastole.",
    "EF": "Ejection fraction (%). Heart pumping strength. The key echo number.",
    "Aortic Valve": "Aortic valve finding (narrative).",
    "Mitral Valve": "Mitral valve finding (narrative).",
    "Tricuspid Valve": "Tricuspid valve finding (narrative).",
    "Pulmonary Valve": "Pulmonary valve finding (narrative).",
    "Left Atrium": "Left atrium finding (narrative).",
    "Right Atrium": "Right atrium finding (narrative).",
    "Left Ventricle": "Left ventricle finding (narrative).",
    "Right Ventricle": "Right ventricle finding (narrative).",
    "IAS": "Inter-atrial septum. 'INTACT' is normal.",
    "IVS": "Inter-ventricular septum. 'INTACT' is normal.",
    "Pericardium": "Sac around the heart.",
    "P Duration": "ECG P-wave duration.",
    "PR Interval": "ECG PR interval.",
    "QRS Duration": "ECG QRS duration.",
    "QT Interval": "ECG QT interval.",
    # Sleep therapy (BiPAP / CPAP)
    "AHI": "Apnoea-Hypopnoea Index: breathing pauses per hour. THE sleep-apnoea number.",
    "Hypopnea Index": "Shallow-breathing events per hour.",
    "Obstructive Apnea Index": "Obstructed (airway-blocked) pauses per hour.",
    "Clear Airway Index": "Central (brain-driven) pauses per hour.",
    "RERA Index": "Effort-related arousals per hour.",
    "Flow Limitation Index": "Restricted airflow events.",
    "IPAP": "BiPAP inspiratory pressure (breathing in).",
    "EPAP": "BiPAP expiratory pressure (breathing out).",
    "Days with Device Usage": "Nights the BiPAP was used.",
    "Days without Device Usage": "Nights it was not used.",
    "Percent Days with Device Usage": "Compliance %.",
    "Large Leak Percent": "% of night with mask leak. High = poor mask fit.",
    # Imaging / other
    "Abdomen & Pelvis": "Abdominal ultrasound findings (narrative).",
    "Breast": "Breast ultrasound findings (narrative).",
    "Chest PA": "Chest X-ray findings (narrative).",
    "Final Diag": "Final diagnosis / conclusion (narrative).",
    "Impression": "Examiner's impression (narrative).",
    "Vision": "Distance vision.",
    "Near Vision": "Near vision.",
    "Weight": "Body weight (kg).",
    "BP High": "Systolic blood pressure.",
    "BP Low": "Diastolic blood pressure.",
    "SPO2": "Blood oxygen saturation (%).",
    "Pulse": "Heart rate.",
    "Pap Smear": "Cervical screening.",
}


def main() -> None:
    codebook = load_codebook()
    people = load_people()
    con = db.connect()

    have: dict[str, list[tuple[str, int, str, str]]] = defaultdict(list)
    for r in con.execute("""SELECT analyte, subject, count(*) n, min(effective) a, max(effective) b
           FROM observations WHERE analyte IS NOT NULL
           GROUP BY analyte, subject"""):
        rel = people[r["subject"]].relation if r["subject"] in people else r["subject"]
        have[r["analyte"]].append((rel, r["n"], r["a"] or "?", r["b"] or "?"))

    by_segment: dict[str, list[str]] = defaultdict(list)
    for name, entry in codebook.items():
        by_segment[entry.get("segment") or "(no segment)"].append(name)

    lines = [
        "# nalam — analyte codebook for review",
        "",
        f"{len(codebook)} analytes.",
        "",
        "**The lab's own printed range is the source of truth** — labs revise their ranges,",
        "and a printed range is specific to the assay actually used and age-appropriate for",
        "children. The per-sex ranges below are only the FALLBACK, used for the ~7% of",
        "results where the lab printed no usable range (or printed an interpretive table",
        "rather than a band).",
        "",
        "`—` in the People column means no data extracted for that analyte yet.",
        "",
    ]
    for segment in sorted(by_segment):
        lines += [
            f"## {segment}",
            "",
            "| Analyte | Meaning | Range (M / F) | People with data |",
            "|---|---|---|---|",
        ]
        for name in sorted(by_segment[segment]):
            e = codebook[name]
            r = e.get("ranges") or {}

            def band(sex: str) -> str:
                b = r.get(sex)
                if not b:
                    return "—"
                lo, hi = b.get("low"), b.get("high")
                return f"{'' if lo is None else lo}–{'' if hi is None else hi}"

            who = have.get(name, [])
            who_s = (
                "<br>".join(f"{rel} ({n}, {a[:4]}–{b[:4]})" for rel, n, a, b in sorted(who)) or "—"
            )
            meaning = MEANING.get(name, "**?? no description — please check**")
            lines.append(
                f"| **{name}** | {meaning} | {band('male')} / {band('female')} | {who_s} |"
            )
        lines.append("")

    missing = [n for n in codebook if n not in MEANING]
    if missing:
        lines += ["## Analytes with no description (please check)", ""]
        lines += [f"- {n}" for n in sorted(missing)]

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote {OUT}")
    print(f"  {len(codebook)} analytes, {len(have)} of them have data")
    print(f"  {len(missing)} without a description")


if __name__ == "__main__":
    main()
