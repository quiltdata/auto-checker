Checked `quilt+s3://{{ report.registry | replace("s3://", "") }}#package={{ report.package }}@{{ report.tophash }}`
{% if report.prev_tophash %}
against its parent `@{{ report.prev_tophash[:12] }}`.
{% else %}
as the first revision of the package.
{% endif %}
{{ report.checks_run | length }} checks under the `{{ report.regime }}` contract;
verdict: **{{ report.verdict }}** ({{ defect_count }} defect(s), {{ ku_count }} known-unresolved).

{% for f in findings %}
{{ loop.index }}. **[{{ f.severity }}]** `{{ f.check }}/{{ f.kind }}`{{ " — " ~ f.paths | join(", ") if f.paths else "" }}

    {{ f.detail }}

{% endfor %}
{% if ku_count %}
Known-unresolved findings are conditions on record, not defects.

{% endif %}
{% if report.notes %}
Notes:

{% for n in report.notes %}
- {{ n }}
{% endfor %}

{% endif %}
Engine: `check-commit {{ report.engine_version }}`, policy `{{ policy.prefix }}`.

Proposed disposition: findings only. `{{ policy.contributor }}` certifies
nothing and closes nothing; the issue's persistent participant disposes.
