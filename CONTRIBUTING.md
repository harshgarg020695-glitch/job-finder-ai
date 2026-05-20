# Contributing to Job Finder AI

Thanks for helping improve job search for India.

## How to contribute

**Scraper fixes (most needed):** Job platforms change their HTML regularly.
If a scraper breaks, the fix is usually one line in the `SELECTORS` dict.

1. Fork the repo
2. Find the broken selector in `job_automation/naukri_scraper.py`
   (or whichever scraper broke)
3. Update the relevant key in the `SELECTORS` dict at the top of the file
4. Run: `python scrutinizer.py --quick` to verify it works
5. Open a PR with title: `fix: update [platform] selector for [date]`

**New platform scrapers:**
Open an issue first to discuss which platform and approach.
Priority platforms: Instahyre, Hirist, Cutshort.

**Scoring improvements:**
Edit `scoring_config.json` — this file controls scoring signals
for different career domains. No code changes needed.
Test with: `python scrutinizer.py --quick --scenario [your_scenario]`

## Dev setup

```bash
git clone https://github.com/harshgarg95/job-finder-ai
cd job-finder-ai
pip install -r requirements.txt
cp .env.example .env    # add your keys
python scrutinizer.py --quick
```

## What not to do

- Don't add paid API dependencies
- Don't commit `.env` or any API keys
- Don't change `MIN_FIT_SCORE` without running all 5 scrutinizer scenarios
