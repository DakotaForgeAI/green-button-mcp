# green-button-mcp

A small, read-only [MCP](https://modelcontextprotocol.io) server that lets Claude answer questions about your electricity usage from a **Green Button "Download My Data"** file: the XML file (NAESB ESPI format) that many utilities let you download from their website.

You give Claude the path to your file. It can then tell you how much you used per day, when in the day you use the most, which days were highest, how two periods compare, and roughly what that usage cost at a rate you supply.

## What it does

| Tool | What it returns |
| --- | --- |
| `summarize_file(path)` | Date range, number of readings, interval length, total kWh, time zone, and any gaps or duplicates |
| `daily_usage(path, start?, end?)` | kWh per local calendar day (DST-aware), with how complete each day is |
| `hourly_profile(path, start?, end?)` | Average kWh for each hour of the day |
| `peak_days(path, n=5)` | The highest-usage days |
| `compare_periods(path, a_start, a_end, b_start, b_end)` | kWh totals, per-day averages, and % change from A to B |
| `estimate_cost(path, start, end, rate_per_kwh, monthly_fixed=0)` | A simple cost **estimate** from rates you supply |

Dates are `YYYY-MM-DD` and inclusive. Days and hours are in the file's local time zone, taken from its `LocalTimeParameters`, including daylight saving time. A spring-forward day has 23 hours and a fall-back day has 25.

**How the data is handled**

- Values are scaled by the file's `powerOfTenMultiplier` and `uom` (Wh → kWh).
- Multiple interval blocks are merged. Exact duplicate readings are removed and counted. If duplicates disagree, the one later in the file is kept, and the tool says so.
- **Gaps are reported, never filled in.** Every tool says how many intervals are missing, and incomplete days and periods are flagged as undercounted.
- If the file also has a "received" series (for example, solar export), it is listed but kept out of the usage totals.
- `.zip` downloads work too. The XML inside is read in memory.
- `estimate_cost` uses only the rates you supply. It has no utility rate tables and leaves out taxes, riders, tiers, time-of-use, and demand charges. It is clearly labeled as an estimate and is not a bill.

**Privacy**

- Read-only: the server never writes, uploads, or sends data anywhere, and it makes no network requests.
- It reads only the file path you give it.
- It never logs usage values.
- Keep in mind that the results the tools return (daily totals and so on) become part of your conversation with Claude, like anything else you share in a chat.

## Install

Requires Python 3.10+.

**With [uv](https://docs.astral.sh/uv/)** (recommended):

```bash
git clone https://github.com/DakotaForgeAI/green-button-mcp
```

```bash
cd green-button-mcp && uv sync
```

**With pip:**

```bash
cd green-button-mcp && python3 -m venv .venv && .venv/bin/pip install .
```

## Claude Desktop configuration

Open Claude Desktop → Settings → Developer → Edit Config, or edit the file directly:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

Using uv (replace the path with where you cloned the repo):

```json
{
  "mcpServers": {
    "green-button": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/green-button-mcp", "run", "green-button-mcp"]
    }
  }
}
```

Using a pip virtualenv:

```json
{
  "mcpServers": {
    "green-button": {
      "command": "/ABSOLUTE/PATH/TO/green-button-mcp/.venv/bin/green-button-mcp"
    }
  }
}
```

Restart Claude Desktop afterwards. If it can't find `uv`, use the full path from `which uv`.

## Example questions

Tell Claude where the file is, for example `~/Downloads/usage.xml`, then ask:

1. "Summarize my Green Button file at ~/Downloads/usage.xml. Are there any gaps in the data?"
2. "What were my five highest-usage days, and what day of the week were they?"
3. "What time of day do I use the most electricity?"
4. "How did my usage the week of Nov 10 compare with the week of Oct 20?"
5. "At 12 cents per kWh plus a $10 monthly charge, roughly what did I spend in the first week of November?"

## Try it without your own data

The repo includes a **synthetic** sample, `samples/synthetic_green_button.xml`: four weeks of 15-minute readings in US Central time. It crosses the November DST change and includes one deliberate 3-hour gap and a few duplicated readings. Every value comes from a seeded random model, so none of it is real meter data. To regenerate it:

```bash
uv run python scripts/generate_sample.py
```

Ask Claude to "summarize samples/synthetic_green_button.xml" using the full path.

## How to get your Green Button file

Many US and Canadian utilities offer Green Button **Download My Data**:

1. Sign in to your utility's website or account portal.
2. Go to the energy-usage section and look for **"Green Button"**, **"Download My Data"**, or **"Download usage."**
3. Choose the date range and the **XML** format (not CSV). Choose 15-minute or hourly intervals if you're asked.
4. Save the file (often a `.zip`) somewhere on your computer, and give Claude its path.

Where the option is found, and which date ranges are offered, varies by utility. If you can't find it, search your utility's help pages for "Green Button."

Your downloaded file contains your real usage. This repo's `.gitignore` excludes all `*.xml` and `*.zip` files except the synthetic sample, so you don't commit it by accident.

## Development

```bash
uv sync --extra dev && uv run pytest
```

The tests cover parsing (multipliers, multiple blocks, duplicates, gaps, zip files, unsafe XML), DST (rule decoding, checks against the system time zone database across four years, 23- and 25-hour days), and each tool.

## Acknowledgments

Thanks to [VerdigrisTech/green-button-data](https://github.com/VerdigrisTech/green-button-data), whose documentation of the ESPI DST rule bit layout was a helpful reference.

---

Want this automatic, with no file downloads? Dakota Forge Energy (energy.dakotaforge.ai) does it for Xcel Energy customers in South Dakota.

**Disclaimer:** Not affiliated with or endorsed by Xcel Energy or the Green Button Alliance. These names are used only to describe compatibility.

License: [MIT](LICENSE) © 2026 Dakota Forge Energy LLC
