"""Verify generated import files against Frappe v16 Data Import rules and the source.

Checks, per file:
* one sheet only (Frappe reads only the active sheet);
* every header resolves to a real field, using the importer's own key rules
  (parent: Label / fieldname / "Label (fieldname)"; child: "Label (Table Label)");
* rows group into documents exactly like importer.parse_next_row_for_import;
* mandatory parent fields are present, Select values are valid, names unique
  (case-insensitive, as MariaDB compares names);
* every Link value exists in an earlier file (import order);
* round trip: every source Compound/Phase, parent link and checked amenity is
  present in the output.
"""

import collections
import json
import sys
from pathlib import Path

import openpyxl

# Resolved from this file so the tool works from any checkout:
# <repo>/tools/compound_import/verify_import_workbooks.py -> <repo>/real_estate_crm_customs/...
DOCTYPES = (
    Path(__file__).resolve().parents[2]
    / "real_estate_crm_customs"
    / "real_estate_crm_customs"
    / "doctype"
)
NO_VALUE = {"Section Break", "Column Break", "Tab Break", "HTML", "Button", "Image", "Fold", "Heading"}


def meta(folder):
    return json.loads((DOCTYPES / folder / f"{folder}.json").read_text())


def header_map(parent_folder):
    parent = meta(parent_folder)
    out = {}
    for df in parent["fields"]:
        if df["fieldtype"] in NO_VALUE:
            continue
        label = (df.get("label") or "").strip()
        out.setdefault(label, ("parent", df))
        for key in (df["fieldname"], f"{label} ({df['fieldname']})"):
            out[key] = ("parent", df)
    for table_df in parent["fields"]:
        if table_df["fieldtype"] != "Table":
            continue
        child_folder = table_df["options"].lower().replace(" ", "_")
        table_ref = table_df.get("label") or table_df["fieldname"]
        for df in meta(child_folder)["fields"]:
            if df["fieldtype"] in NO_VALUE:
                continue
            label = (df.get("label") or "").strip()
            for key in (f"{table_df['fieldname']}.{df['fieldname']}", f"{label} ({table_ref})"):
                out[key] = ("child", dict(df, table=table_df["fieldname"]))
    return parent, out


def read(path):
    book = openpyxl.load_workbook(path, read_only=True)
    assert len(book.sheetnames) == 1, f"{path.name}: more than one sheet"
    rows = list(book.active.iter_rows(values_only=True))
    return [str(h) for h in rows[0]], [list(r) for r in rows[1:]]


def blank(value):
    return value is None or str(value).strip() == ""


def documents(path, parent_folder):
    parent, hmap = header_map(parent_folder)
    headers, rows = read(path)
    unknown = [h for h in headers if h not in hmap]
    assert not unknown, f"{path.name}: unmatched headers {unknown}"
    columns = [hmap[h] for h in headers]
    parent_idx = [i for i, (kind, _df) in enumerate(columns) if kind == "parent"]
    docs, current = [], None
    for row in rows:
        if all(blank(row[i]) for i in parent_idx):
            assert current is not None, f"{path.name}: child row before any parent row"
        else:
            current = {"_children": collections.defaultdict(list)}
            docs.append(current)
            for i in parent_idx:
                current[columns[i][1]["fieldname"]] = None if blank(row[i]) else row[i]
        for i, (kind, df) in enumerate(columns):
            if kind == "child" and not blank(row[i]):
                current["_children"][df["table"]].append(row[i])

    fields = {df["fieldname"]: df for df in parent["fields"]}
    for doc in docs:
        for fieldname, df in fields.items():
            if df.get("reqd") and fieldname in doc and blank(doc[fieldname]):
                raise AssertionError(f"{path.name}: {doc} missing mandatory {fieldname}")
        for fieldname, value in doc.items():
            df = fields.get(fieldname)
            if df and df["fieldtype"] == "Select" and value is not None:
                assert str(value) in df["options"].split("\n"), f"{path.name}: bad {fieldname}={value}"
    name_field = parent["autoname"].split(":", 1)[1]
    names = [str(doc[name_field]).strip() for doc in docs]
    for name in names:
        assert "<" not in name and ">" not in name, f"bad name {name}"
    return docs, names, name_field


def main(output_dir, source):
    out = Path(output_dir)
    report = {}
    dest, dest_names, _ = documents(out / "1_Destinations.xlsx", "real_estate_destination")
    devs, dev_names, _ = documents(out / "2_Developers.xlsx", "property_developer")
    amen, amen_names, _ = documents(out / "3_Amenities.xlsx", "real_estate_amenity")
    comps, comp_names, _ = documents(out / "4_Compounds.xlsx", "real_estate_project")
    phases, phase_names, _ = documents(out / "5_Phases.xlsx", "real_estate_project")

    all_project = [n.lower() for n in comp_names + phase_names]
    dupes = [n for n, c in collections.Counter(all_project).items() if c > 1]
    assert not dupes, f"duplicate Compound/Phase names: {dupes}"
    for label, names in (("destinations", dest_names), ("developers", dev_names), ("amenities", amen_names)):
        lowered = [n.lower() for n in names]
        assert len(lowered) == len(set(lowered)), f"duplicate {label}"

    dest_set, dev_set, amen_set = set(dest_names), set(dev_names), set(amen_names)
    comp_set = set(comp_names)
    for doc in comps + phases:
        assert doc["destination"] in dest_set, doc
        assert doc["developer"] in dev_set, doc
        for amenity in doc["_children"].get("amenities", []):
            assert amenity in amen_set, (doc["project_name"], amenity)
        amen_list = doc["_children"].get("amenities", [])
        assert len(amen_list) == len(set(amen_list)), f"duplicate amenity on {doc['project_name']}"
    for doc in comps:
        assert doc["record_type"] == "Compound" and not doc.get("parent_compound"), doc
    for doc in phases:
        assert doc["record_type"] == "Phase", doc
        assert doc["parent_compound"] in comp_set, (doc["project_name"], doc["parent_compound"])

    # Round trip against the source workbook.
    sheet = openpyxl.load_workbook(source, read_only=True).active
    src_rows = list(sheet.iter_rows(values_only=True))
    headers = [" ".join(str(h or "").split()) for h in src_rows[0]]
    src = [dict(zip(headers, r)) for r in src_rows[1:] if any(v not in (None, "") for v in r)]
    by_url = {doc["source_url"]: doc for doc in comps + phases}
    assert len(by_url) == len(src) == len(comps) + len(phases)
    amen_cols = headers[10:]
    fixes = json.loads((out / "import_summary.json").read_text())["amenity_name_fixes"]
    url_to_name = {d["source_url"]: d["project_name"] for d in comps + phases}
    checked_links = 0
    for row in src:
        doc = by_url[row["compound_url"].strip()]
        expected = {fixes.get(c, c) for c in amen_cols if str(row.get(c) or "").strip() == "✓"}
        assert set(doc["_children"].get("amenities", [])) == expected, doc["project_name"]
        checked_links += len(expected)
        assert doc["developer"] == " ".join(str(row["developer"]).split())
        assert doc["destination"] == " ".join(str(row["area"]).split())
        if str(row["record_type"]).strip().lower() == "phase":
            assert doc["parent_compound"] == url_to_name[row["parent_compound_url"].strip()]
        if row.get("master_plan_urls"):
            assert doc["master_plan"] == str(row["master_plan_urls"]).strip()

    report.update(
        destinations=len(dest), developers=len(devs), amenities=len(amen),
        compounds=len(comps), phases=len(phases), amenity_links_checked=checked_links,
        source_rows=len(src),
    )
    print("ALL CHECKS PASSED")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
