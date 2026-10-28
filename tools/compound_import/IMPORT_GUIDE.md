# Compound, Phase and Amenity Import Guide

This guide explains how to load your file `nawy_areas_compounds_developers.xlsx` into the CRM. It covers 802 Compounds, 548 Phases, 247 Developers, 9 Destinations and 27 Amenities.

Your original sheet cannot be imported as it is. Frappe's Data Import reads **one sheet, for one document type**, and **every linked value must already exist**. So the data has been split into five ready-made files that must be imported **in order**. Each file uses the exact column headers that Frappe v16 recognises.

## 1. Deploy the update first

The files depend on fields added in this release, so deploy before importing:

| Repository | Branch | Commit | What it adds |
| --- | --- | --- | --- |
| `crm-Real-State-Customs` (custom app) | `main` | `bd83408` | Record Type (Compound/Phase), Parent Compound, Source URL, Unit Category master, Master Compound on Units, and removal of the import blockers |
| `crm-Real-State` (CRM portal) | `develop` | `8aa306b7` | Phases panel on the Compound form, Unit Categories menu, dependent filters, and every real-estate master on the Data Import page |

On Frappe Cloud, deploy the custom app first and let **migrate** finish. Then deploy the portal build, and hard-refresh the browser (Ctrl+Shift+R) so the new menus appear.

Migrate runs the patch `compound_phases_and_unit_categories`. The patch is safe to run more than once and deletes nothing. It marks existing Compounds as `Record Type = Compound`, fills Master Compound and Unit Category on existing Units, and seeds the default Unit Categories and Unit Types once.

## 2. Import the five files in this order

For each file, open **Data Import → New** (or use **Import** in the list menu of that DocType). Choose **Insert New Records** and upload the file. Check the preview, then start the import. Wait for **Success** before moving to the next file.

| Step | File | Import into (DocType) | Rows | Records created |
| --- | --- | --- | --- | --- |
| 1 | `1_Destinations.xlsx` | Real Estate Destination | 9 | 9 Destinations, all Active |
| 2 | `2_Developers.xlsx` | Property Developer | 247 | 247 Developers (only the name is required) |
| 3 | `3_Amenities.xlsx` | Real Estate Amenity | 27 | 27 Amenities |
| 4 | `4_Compounds.xlsx` | Real Estate Project (Compounds) | 2,485 | 802 Compounds with 2,144 Amenity links |
| 5 | `5_Phases.xlsx` | Real Estate Project (Compounds) | 1,462 | 548 Phases with 1,211 Amenity links |

**Phases must be imported after Compounds.** Each Phase links to its Parent Compound, and that Compound must already exist.

## 3. How the Compound and Phase files are laid out

Each Compound or Phase starts on a row with its main columns filled in. Its first Amenity is on that same row. Each extra Amenity goes on the next row, with all main columns left **blank**. Frappe adds those rows to the Amenities table of the record above. In the files, the green header marks the child-table column.

| Compound Name | Record Type | Parent Compound | Developer | Destination | Master Plan | Source URL | Amenity (Amenities) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Mountain View 1.1-Phase 2 | Phase | Mountain View 1.1 | Mountain View | New Cairo | https://…jpg | https://www.nawy.com/… | Clubhouse |
| | | | | | | | Outdoor Pools |
| | | | | | | | Jogging Trail |

Here is how each column was built from your sheet:

| Your column | Imported as | Notes |
| --- | --- | --- |
| `compound_name` | Compound Name | Becomes the record name |
| `record_type` (compound / phase) | Record Type | Written as `Compound` or `Phase` |
| `PARENT COMPOUND` / `parent_compound_url` | Parent Compound | Matched by URL, so names with extra spaces still link correctly |
| `developer` | Developer | |
| `area` | Destination | |
| `master_plan_urls` | Master Plan | Stored as a link to the image URL; the image is not downloaded |
| `compound_url` | Source URL | Keeps the original listing page |
| 27 check-mark (✓) columns | Amenities child table | One Amenity row per ✓ |

## 4. Data corrections made while building the files

The files were checked against your sheet: all 1,350 rows, all 548 Phase-to-Compound links and all 3,355 check marks match. Only two kinds of changes were made.

**Duplicate name.** Your sheet has two different projects called *La Vista City*. One is by La Vista Developments and the other is `LA VISTA CITY` by STM Developments. Frappe treats names as case-insensitive, so the second one would have failed. It is imported as **La Vista City (STM Developments)**. You can rename it later from the Compound form.

**Amenity names made consistent.**

| In your sheet | Imported as |
| --- | --- |
| Outdoor pools | Outdoor Pools |
| Commercial strip | Commercial Strip |
| Jogging trail | Jogging Trail |
| bicycles lanes | Bicycle Lanes |
| Underground parking | Underground Parking |
| Medical center | Medical Center |
| Disability support | Disability Support |

All 27 amenity columns are kept. A few look more like data points than amenities: *Project Area*, *Finishing*, *Footprint*, *Infrastructure* and *Livability*. Remove them from `3_Amenities.xlsx` and from the Amenity column of files 4 and 5 before importing if you do not want them as amenities.

## 5. What the system does for Phases

- A Phase **inherits Developer and Destination** from its Parent Compound. Those fields are read-only on a Phase. If the Compound's Developer or Destination changes later, its Phases and their Units update too.
- A Phase cannot be its own parent, and it cannot belong to another Phase. A Compound that has Phases cannot be deleted until its Phases are removed or moved.
- The Compound form lists its Phases in a **Phases** panel. **Add Phase** opens a new Phase with the Parent Compound already filled in.
- A Unit may be attached to a Compound or to a Phase. Its read-only **Master Compound** always shows the top-level Compound. When agents filter Unit selection or Smart Match by a Compound, Units in its Phases are included. Picking a specific Phase shows only that Phase's Units.
- Compound Status is now optional. Imported records keep it blank instead of getting a made-up *Planning* status.

## 6. If a row fails

Data Import reports each failed row and still imports the others. Common causes:

| Message | Cause | Fix |
| --- | --- | --- |
| Duplicate entry / already exists | The record is already in the system, e.g. a Destination you created earlier | Nothing to do; the existing record is used |
| Could not find Destination / Developer / Amenity | Files were imported out of order | Import files 1–3 first, then re-run the failed rows |
| Parent Compound … was not found | Phases were imported before Compounds finished | Finish file 4, then re-import the failed Phase rows |
| Select an active Destination | An existing Destination is marked inactive | Tick **Active** on that Destination, then retry |

To retry, download the failed rows from the import log, fix them, and import that smaller file the same way.

## 7. Regenerating the files

If you edit the source sheet, rebuild and re-check the five files with the two scripts that come with this guide:

```bash
# run from the repository root
python3 -m pip install openpyxl
python3 tools/compound_import/build_import_workbooks.py nawy_areas_compounds_developers.xlsx import_files
python3 tools/compound_import/verify_import_workbooks.py import_files nawy_areas_compounds_developers.xlsx
```

The verifier uses the same header and row-grouping rules as the Frappe v16 importer. It confirms that every Link value appears in an earlier file, and it checks the files against your sheet row by row.
