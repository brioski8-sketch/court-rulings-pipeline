#!/bin/bash
# Thin delegator. The canonical pipeline lives in ~/.hermes/scripts/ because that is the
# path the Monday cron runs, and it is the copy that exports CANLII_API_KEY and drives all
# six steps. Keeping a second implementation here caused drift once already (this file was
# missing the Ontario, federal and SCC-text steps entirely), so it now just forwards.
exec "$HOME/.hermes/scripts/court-rulings-pipeline.sh" "$@"
