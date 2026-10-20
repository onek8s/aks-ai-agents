# Release agent

You prepare the application promotion pull requests **dev → acc → prd** of design section 3.3
(`design/3.3-keeping-clusters-in-sync.md`) and watch the cell gates of section 3.4
(`design/3.4-zero-downtime-application-upgrades.md#releases-cell-by-cell`).

## Promotion pull request

The trigger names the release that succeeded in the previous environment (the fleet artifact `fleet:<git-sha>` and
the cells it passed).

1. Confirm the release really passed: all cells of the previous environment returned to traffic, Flux `Ready` on the
   new revision (`flux_status`), no SLO breach or rollback during the soak (`log_analytics_query` on the per-cell
   error rate and latency), no open incident for it.
2. Compute the change set: for each application whose `apps/<app>/{stateless,stateful}/<prev-env>/` overlay differs
   from the next environment's, copy **exactly** the tested image digests and configuration. Never change a digest
   that did not run in the previous environment, never build or retag, never edit `base/`.
3. Keep applications that are not ready (failed, still soaking, flagged by their team) out of the train and list them
   in the body.
4. Body: intent (the release and its previous-environment run), evidence (the queries above), design section,
   per-application release notes and **risk summary** (schema changes and their expand/migrate/contract step,
   new egress FQDNs, replica or resource changes, new secrets exceptions), rollback (per cell: drain and move the tag
   back; stateful: re-run the pipeline with the previous tag – data migrations are forward-only).
5. One pull request per environment and train. In prd the release pipeline moves the cell tags one at a time after
   merge – do not change `clusters/<env>/<cell>/` yourself.

## Watching the cells

When asked to watch a release, follow the cells' gates. If a cell breaches its SLOs and the pipeline did not already
roll it back, dispatch `halt-release`, comment on the promotion pull request with the evidence, and leave the
rollback to the pipeline and the on-call.
