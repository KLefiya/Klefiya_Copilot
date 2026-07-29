# practical-v2 Migration Workspace Demo

This is a five-minute demo script for the local Migration Review Workspace.

## Preconditions

- Python dependencies are installed.
- Frontend dependencies are installed.
- Ports `8001` and `5173` are available.
- No LLM credential is required.

## Start

Backend:

```powershell
uvicorn backend.main:app --reload --port 8001
```

Frontend:

```powershell
cd frontend
$env:VITE_API_BASE="http://127.0.0.1:8001"
npm run dev
```

Open:

```text
http://127.0.0.1:5173/
```

## Demo Script

1. Open the default Migration Workspace.
2. Show the 10 source fields.
3. Expand `article_number`.
4. Explain that the blind engine had no formal recommendation for multi-target execution.
5. Show the Top-3 candidates and the two `item_code` targets.
6. Keep both `item_code` targets approved.
7. Expand `inventory_measure`.
8. Keep both UOM targets approved.
9. Click save.
10. Explain that Runtime Decision is isolated from the committed Seed Decision.
11. Click build.
12. Show `valid=true` and `findings=0`.
13. Show `item.csv` as 8 rows x 5 fields.
14. Show `item_price.csv` as 8 rows x 6 fields.
15. Filter lineage by `article_number`.
16. Explain that 16 lineage entries come from 8 rows x 2 targets.
17. Click reset.
18. Explain that runtime state is deleted and the Seed Decision is restored.
19. Confirm that the git worktree remains unchanged.

## Presenter Notes

- The model only gives candidates.
- The human reviewer decides the approved links.
- The deterministic builder executes decisions.
- The same contract revalidates generated results.
- Lineage records which source field produced each target cell.

## Failure Demos

- Stale Decision SHA returns `409`.
- Target conflict is rejected before build.
- Unsaved changes disable build.
- Report API `POST` returns `405`.
