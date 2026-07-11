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
- "reason": one line

Return: {{"verdicts": [ ... ]}}
