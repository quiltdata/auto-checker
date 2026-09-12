{{ report.package }} @ {{ report.tophash[:12] }} — {{ report.verdict | upper }}

{{ report.checks_run | length }} checks under the `{{ report.regime }}` contract.
{% if report.prev_tophash %}
Parent: {{ report.prev_tophash[:12] }}
{% else %}
First revision of the package.
{% endif %}
{% if report.pointer %}
Pointer: {{ report.pointer }}
{% endif %}
{% if report.error %}

## Engine error

{{ report.error }}
{% endif %}
{% if defects %}

## Defects ({{ defects | length }})
{% for f in defects %}

{{ loop.index }}. {{ f.check }} / {{ f.kind }}
{% for p in f.paths %}
   {{ p }}
{% endfor %}

   {{ f.detail }}
{% endfor %}
{% endif %}
{% if kus %}

## Known-unresolved ({{ kus | length }})

Conditions on record, not defects: the contract states a preference rather than
a requirement, or the evidence needed to decide is not available offline.
{% for f in kus %}

{{ loop.index }}. {{ f.check }} / {{ f.kind }}
{% for p in f.paths %}
   {{ p }}
{% endfor %}

   {{ f.detail }}
{% endfor %}
{% endif %}
{% if report.notes %}

## Notes

Observations that set no verdict.
{% for n in report.notes %}

- {{ n }}
{% endfor %}
{% endif %}
{% if not defects and not kus and not report.error %}

No findings.
{% endif %}

--
{% if catalog_url %}
{{ catalog_url }}
{% endif %}
check-commit {{ report.engine_version }}, policy `{{ policy.prefix }}`, regime `{{ report.regime }}`.
Severity tracks the contract: a defect cites a rule, known-unresolved records a
condition, a note sets no verdict.
