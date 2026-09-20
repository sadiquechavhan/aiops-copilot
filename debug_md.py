from markdown_it import MarkdownIt

md = MarkdownIt()
text = """# Runbook: Inventory Service Pool Exhaustion

## Fault Type
Pool exhaustion in inventory service

## Symptom
- HikariCP used rises

## Diagnosis
1. Check query_metrics
"""
tokens = md.parse(text)
for t in tokens:
    print(f'{t.type:30} {t.tag:15} {t.level:2} {t.content[:50] if t.content else ""}')