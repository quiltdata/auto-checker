Checked `quilt+s3://{{ report.registry | replace("s3://", "") }}#package={{ report.package }}@{{ report.tophash }}`
against its parent `@{{ report.prev_tophash[:12] }}`. {{ report.checks_run | length }} checks;
verdict: **{{ report.verdict }}** ({{ defect_count }} defect(s), {{ ku_count }} known-unresolved).

{% for f in findings %}
{{ loop.index }}. **[{{ f.severity }}]** `{{ f.check }}/{{ f.kind }}` — {{ f.paths | join(", ") }}
   {{ f.detail }}
{% endfor %}
{% if ku_count %}
Known-unresolved findings are reported per `{{ policy.adjudication_cite }}`;
they are conditions on record, not defects.
{% endif %}
{% if report.notes %}
Notes:
{% for n in report.notes %}
- {{ n }}
{% endfor %}
{% endif %}

Engine: `check-commit {{ report.engine_version }}`, policy `{{ policy.prefix }}`.

Proposed disposition: findings only. {{ policy.cast_label }} certifies nothing
and closes nothing; a human disposes.
