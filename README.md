# pfasimplu-cli

A standalone, local-first **Typer CLI** that turns a ZIP of PFA documents into
the final Romanian fiscal-register ZIP.

The only cloud service is **Google Gemini**. The input ZIP, extracted documents,
reports, registers, configuration and logs stay on your machine.

---

## One-command install

**Ubuntu / Debian / macOS** (POSIX shell):

```bash
curl -LsSf https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/install.sh | sh
```

**Windows** (PowerShell):

```powershell
irm https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/install.ps1 | iex
```

The installer downloads `uv` when needed, installs `pfasimplu-cli` in an
isolated `uv tool` environment, and prints the next steps. Then:

```bash
pfasimplu-cli init                   # write pfasimplu.toml
export GOOGLE_GENAI_KEY="your-key"   # or put it in ./.env
pfasimplu-cli doctor                 # verify config, key and dependencies
pfasimplu-cli process ./documente.zip
```

Installer options (when piping, pass them after `sh -s --` on Unix):

| Option | Description |
| --- | --- |
| `--ref REF` | branch, tag or commit to install (default: `main`) |
| `--upgrade` | reinstall/refresh an existing installation |
| `--python PY` | Python version for the tool environment (default: `3.12`) |
| `--repo URL` | install from a fork (git URL, or a local path) |
| `--help` | show the installer help |

Unix installers can also be configured with `PFASIMPLU_REPO_URL`,
`PFASIMPLU_REF` and `PFASIMPLU_PYTHON`; the PowerShell installer reads the same
variables.

### Uninstall

```bash
# Ubuntu / macOS
curl -LsSf https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/uninstall.sh | sh

# Windows PowerShell
irm https://raw.githubusercontent.com/ClimenteA/pfasimplu-cli/main/uninstall.ps1 | iex
```

This removes the CLI installed with `uv tool`. `uv` itself and your `.env` are
left alone. Generated registers stay in `./output`; add `--purge-data` to also
remove `./pfasimplu.toml` and `./output` from the current directory.

---

## Quick start

After the one-command install:

```bash
# 1. Add the Gemini key in the folder where you run the CLI
printf 'GOOGLE_GENAI_KEY=your-key-here\n' > .env

# 2. Create pfasimplu.toml (interactive)
pfasimplu-cli init

# 3. Process a ZIP
pfasimplu-cli process ./documente.zip
```

The first `process` run in a directory without `pfasimplu.toml` also asks a few
questions and writes the config there. Later runs reuse it.

The result is written to `./output/`:

```
output/
├── registre_pfa_documente_20261025_153033.zip
└── registre_pfa_documente_20261025_153033.json
```

The input ZIP is **never** modified or deleted.

---

## How it works, in plain words

You do not need to sort, rename or open any document. Put everything in one
ZIP and let the tool do the paperwork:

1. **You hand over one ZIP** with all the documents of the period — photos of
   receipts, invoices (PDF or photo), e-invoices (PDF + XML pairs) and bank
   statements.
2. **The tool opens each file and reads it** — amounts, dates, names, CUI/CIF,
   VAT — the same way a person would, then puts every document into the right
   pile: *bonuri fiscale*, *facturi fiscale*, *e-facturi*, *extrase bancare*.
3. **Each pile gets its own summary sheet** (an `.xlsx` file), so you can check
   what was recognised before the final registers are built.
4. **The sheets are merged, duplicates are removed and everything is put in
   order** — by year, then by month, then by date inside the month.
5. **The final registers are generated** and packed into one ZIP: one set of
   Excel files for every year found in your documents, plus a *fișă de mijloc
   fix* for each purchase that needs one — ready to print or hand to the
   accountant.

The ZIP you receive contains:

```
registrul_jurnal_de_incasari_si_plati_pentru_anul_<year>.xlsx   # RJIP
registrul_de_evidenta_fiscala_pentru_anul_<year>.xlsx           # REF
registrul_inventar_pentru_anul_<year>.xlsx                      # RI
fmj_<...>.xlsx                                                 # fișe mijloc fix
```

> ⚠️ **Always do a final check.** The documents are read automatically, so a
> blurry photo, a stamp over the total or an unusual layout can produce a wrong
> amount or date. Before you file anything or send the registers to the
> accountant, open the Excel files and compare the totals with your own
> documents — especially amounts, dates and CUI/CIF. The tool prepares the
> paperwork; the responsibility for what is submitted stays with you.

A few things worth knowing:

- **Nothing is overwritten.** Each run writes a new ZIP named with the date and
  time, so previous results stay where they are.
- **Bad files do not stop the run.** A blurry photo or an unreadable PDF is
  logged and skipped; the rest of the documents are still processed.
- **The per-type summary sheets are temporary by default.** Add
  `--keep-workdir` to a run if you want to open them afterwards; the command
  prints the folder where they were kept.
- **Documents in other currencies** are converted to RON using the BNR rate for
  the document date (needs internet).

---

## What it does

Four extraction agents run in two parallel batches:

| Stage | Agent | Report |
| --- | --- | --- |
| Bonuri fiscale | `AgentBonuriFiscale` | `raport_bonuri_fiscale.xlsx` |
| E-facturi | `AgentEFacturi` | `raport_efactura.xlsx` |
| Facturi fiscale | `AgentFacturiFiscale` | `raport_facturi_fiscale.xlsx` |
| Extrase bancare | `AgentExtraseBancare` | `raport_extrase_bancare.xlsx` |

The orchestrator then builds:

- RJIP — Registrul de încasări și plăți
- REF — Registrul de evidență fiscală
- RI — Registrul inventar
- Fișe de mijloc fix (`fmj_*.xlsx`) when a purchase crosses the legal threshold

The final ZIP contains the registers. A JSON summary with document counts,
timing and paths is written next to it.

---

## Installation

For normal use, prefer the [one-command installer](#one-command-install) above.
The manual route below is useful for development or when you already have a
checkout.

Requirements:

- Python 3.12+
- A Gemini API key
- Internet access for Gemini, and for BNR exchange rates when a document is not
  in RON (`cursvalutarbnr`)

```bash
# runtime dependencies
uv sync

# development tools (pytest, ruff)
uv sync --extra dev
```

`uv sync` creates `.venv/` for you. Prefix commands with `uv run`, or activate
the environment with `source .venv/bin/activate` and call `pfasimplu-cli`
directly.

You can also run it without installing:

```bash
uv run python -m pfasimplu_cli --help
```

---

## Configuration

### First run

`pfasimplu-cli process` creates `pfasimplu.toml` in the current directory when it is
missing. You can also create it explicitly:

```bash
pfasimplu-cli init
```

The questionnaire asks for:

- company name / PFA name
- CUI / CIF
- VAT code (optional)
- Gemini model (default: `gemini-3.8-flash`)
- output directory (default: `./output`)

Overwrite an existing config with:

```bash
pfasimplu-cli init --force
```

Write it somewhere else with:

```bash
pfasimplu-cli init --config ./configs/client.toml
```

### Example `pfasimplu.toml`

```toml
[company]
nume_firma = "PFA Exemplu"
cui_cif = "1234567890"
cod_tva = ""

[processing]
model = "gemini-3.8-flash"
output_dir = "./output"
keep_workdir = false

[gemini]
# api_key = "..."   # optional; GOOGLE_GENAI_KEY in environment/.env is preferred
```

`[company].nume_firma` and `[company].cui_cif` are required.

### Credentials

The key is read from `GOOGLE_GENAI_KEY` in the environment or from `.env` in
the current directory (and next to the config file). No other variable name is
recognised.

Example `.env`:

```dotenv
GOOGLE_GENAI_KEY=your-gemini-api-key-here
```

The CLI never prints the key.

---

## CLI reference

### `pfasimplu-cli process`

Process one ZIP and write the registre ZIP plus a JSON summary.

```bash
pfasimplu-cli process <input.zip> [options]
```

Options:

| Option | Description |
| --- | --- |
| `--config PATH`, `-c PATH` | Use a specific `pfasimplu.toml` |
| `--output-dir DIR`, `-o DIR` | Override the configured output directory |
| `--model NAME` | Override the Gemini model |
| `--verbose`, `-v` | Enable debug logging |
| `--keep-workdir` | Keep temporary working files, including the per-type `raport_*.xlsx` sheets |
| `--log-file PATH` | Also write logs to a file |

Examples:

```bash
# normal run
pfasimplu-cli process ./documente.zip

# explicit config and output directory
pfasimplu-cli process ./documente.zip \
  --config ./configs/client.toml \
  --output-dir ./rezultate

# debug a run and keep intermediate files
pfasimplu-cli process ./documente.zip --verbose --keep-workdir

# save the full log
pfasimplu-cli process ./documente.zip --log-file ./logs/run.log
```

During a long run the CLI prints stage-based logs:

```
[1/4] Procesare documente si construire registre...
Bonuri fiscale: Extragem bonurile fiscale din format .jpg, .jpeg, .png...
Facturi fiscale: Extragem facturile fiscale din format .pdf, .jpg, .jpeg, .png...
E-facturi: Extragem e-facturile in grupuri de pdf+zip...
Extrase bancare: Procesam extrasul bancar '...'...
[2/4] Mutare arhivei finale in directorul de rezultate...
[3/4] Scriere sumar JSON...
[4/4] Rezultat: .../output/registre_pfa_...zip
Documente: 42 (bonuri 12, facturi 18, e-facturi 8, extrase 4)
Durata: 73.4s
```

### `pfasimplu-cli init`

Create or overwrite `pfasimplu.toml` with the interactive questionnaire.

```bash
pfasimplu-cli init
pfasimplu-cli init --force
pfasimplu-cli init --config ./configs/client.toml
```

### `pfasimplu-cli doctor`

Check Python, dependencies, config, Gemini key and output writability.

```bash
pfasimplu-cli doctor
pfasimplu-cli doctor --check-api      # also make one tiny Gemini request
```

### `pfasimplu-cli config show`

Print the effective configuration with the API key redacted.

```bash
pfasimplu-cli config show
pfasimplu-cli config show --config ./configs/client.toml
```

### `pfasimplu-cli config path`

Print the config file that would be used.

```bash
pfasimplu-cli config path
```

### `pfasimplu-cli version`

```bash
pfasimplu-cli version
```

---

## Output

```
output/
├── registre_pfa_<input-name>_<timestamp>.zip
└── registre_pfa_<input-name>_<timestamp>.json
```

The JSON summary looks like this:

```json
{
  "success": true,
  "input_zip": "/home/user/documente.zip",
  "output_zip": "/home/user/output/registre_pfa_documente_20261025_153033.zip",
  "output_json": "/home/user/output/registre_pfa_documente_20261025_153033.json",
  "total_documente": 42,
  "total_documente_bonuri": 12,
  "total_documente_facturi": 18,
  "total_documente_efacturi": 8,
  "total_documente_extrase": 4,
  "duration_seconds": 73.4,
  "started_at": "2026-10-25T15:30:33",
  "finished_at": "2026-10-25T15:31:46",
  "error": null
}
```

A failed job writes `registre_pfa_<input-name>_<timestamp>_error.json` and exits
with a non-zero status.

---

## Input layout

The input is a ZIP. Nested ZIPs are supported. The agents look for:

- **Bonuri fiscale** — images (`.jpg`, `.jpeg`, `.png`)
- **Facturi fiscale** — PDFs and images
- **E-facturi** — PDF + XML pairs, usually inside ZIPs
- **Extrase bancare** — PDFs

Digital PDFs are read locally with `pypdf`. Scanned PDFs and images are sent to
Gemini vision, replacing the old DocumentAI OCR step.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| `No pfasimplu.toml found` | Run `pfasimplu-cli init`, or run `process` from an interactive terminal so the questionnaire can start. |
| `Gemini API key not found` | Copy `.env.example` to `.env` and set `GOOGLE_GENAI_KEY`, export it, or add `[gemini].api_key` to `pfasimplu.toml`. |
| `Config error` | Check `[company].nume_firma` and `[company].cui_cif` in `pfasimplu.toml`. |
| A single bad document | It is logged and skipped; the rest of the job continues. |
| Non-RON documents | BNR exchange rates are fetched online. Without internet those conversions may fail. |
| Long run looks stuck | Use `--verbose` and/or `--log-file run.log`; each stage logs progress. |

---

## Project layout

```
pfasimplu_cli/
├── cli.py              # Typer commands and first-run questionnaire
├── job.py              # run_job(): orchestration, output ZIP + JSON, timing
├── config.py           # pfasimplu.toml load/write and Gemini key resolution
├── logging.py          # loguru setup
├── ocr.py              # pypdf first, Gemini vision fallback
├── utils.py            # Excel writer, JSON extraction, temp dirs
└── agents/
    ├── bonuri_fiscale.py
    ├── efacturi.py
    ├── extrase_bancare.py
    ├── facturi_fiscale.py
    ├── fisa_mijloc_fix.py
    ├── registre_pfa.py
    └── common.py
```

## Development

```bash
python3 -m compileall pfasimplu_cli
uv run pytest
uv run ruff check .
```
