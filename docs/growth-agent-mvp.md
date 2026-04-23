# One-command growth agent MVP

This MVP ships a narrow paid-test offer workflow in one command:

1. landing-page audit
2. copy rewrite
3. publish checklist

## Command

```bash
python3 tools/growth_agent.py "https://example.com"
```

Output file: `reports/growth-agent-report.md`

## Paid-test defaults

- Offer: **Landing Page Conversion Sprint (Paid Test)**
- Price: **$1,500 fixed**
- Timeline: **5 business days**

Override defaults:

```bash
python3 tools/growth_agent.py "https://example.com" \
  --offer-name "Homepage Conversion Sprint" \
  --offer-price "$2,000 fixed" \
  --offer-timeline "7 days" \
  --output reports/my-run.md
```
