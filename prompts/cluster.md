## Task: cluster adjudication

You are given in-scope card names, Python hint clusters, and wide-track blocked
pairs (in-scope vs Scratch/Archive). Data:

{clusters_json}

For each cluster you judge to be duplicates, return one verdict object with:
- "relation": one of "duplicate", "related-but-distinct", "unrelated"
- "cluster_ids": the card ids in the cluster (only ids from the data above)
- "survivor_id": the card id to keep (richest description, most recent activity,
  highest list position)
- "new_name": a concise merged name using only words/entities present in the
  source cards or spine (or null to keep the survivor's name)
- "merged_desc": a short note on what to consolidate (optional)
- "exact_or_near_name_match": true if the names are exact/near-exact matches
- "llm_tier": 1 if you are highly confident and the merge is safe, else 2
- "confidence": integer 0–100 — your confidence in this merge
- "borderline": true if the user should confirm before merging
- "reason": one line (shown to the user verbatim)

Merged-away duplicate cards move to the top of the Agent Archive list — describe
this as "moved to Trello's archive (restorable)", never as deletion.

Return: {{"verdicts": [ ... ]}}
