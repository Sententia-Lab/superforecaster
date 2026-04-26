"use client";

import {
  Box,
  Card,
  CardActionArea,
  CardContent,
  Chip,
  Stack,
  Typography,
} from "@mui/material";
import Link from "next/link";

import type { ForecastRecord } from "../lib/api";
import {
  brierColor,
  formatDate,
  formatPercent,
  latestProbability,
  relativeFromNow,
} from "../lib/utils";

interface Props {
  forecast: ForecastRecord;
  variant: "active" | "resolved";
}

export function ForecastSummaryCard({ forecast: f, variant }: Props) {
  const current = latestProbability(f.updates);
  const showResolved = variant === "resolved";

  return (
    <Card
      variant="outlined"
      sx={{
        borderColor: f.flagged_for_resolution_review ? "warning.main" : "divider",
        borderWidth: f.flagged_for_resolution_review ? 2 : 1,
      }}
    >
      <CardActionArea component={Link} href={`/forecasts/${f.id}`}>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="flex-start">
            <Box
              sx={{
                minWidth: 90,
                textAlign: "center",
                py: 1,
                px: 1.5,
                borderRadius: 1,
                bgcolor: showResolved ? "background.default" : "action.hover",
              }}
            >
              {showResolved ? (
                <>
                  <Typography variant="caption" color="text.secondary">
                    OUTCOME
                  </Typography>
                  <Typography
                    variant="h2"
                    sx={{
                      color:
                        f.outcome === 1.0
                          ? "success.main"
                          : f.outcome === 0.0
                          ? "error.main"
                          : "text.primary",
                    }}
                  >
                    {f.outcome === 1.0 ? "YES" : f.outcome === 0.0 ? "NO" : "—"}
                  </Typography>
                </>
              ) : (
                <>
                  <Typography variant="caption" color="text.secondary">
                    CURRENT
                  </Typography>
                  <Typography variant="h2" color="primary">
                    {formatPercent(current)}
                  </Typography>
                </>
              )}
            </Box>
            <Box sx={{ flexGrow: 1, minWidth: 0 }}>
              <Stack direction="row" spacing={1} sx={{ mb: 1, flexWrap: "wrap" }}>
                <Chip label={f.category} size="small" variant="outlined" />
                {f.flagged_for_resolution_review && (
                  <Chip label="Resolution flagged" size="small" color="warning" />
                )}
                {showResolved && f.brier_score !== null && (
                  <Chip
                    label={`Brier ${f.brier_score.toFixed(3)}`}
                    size="small"
                    color={brierColor(f.brier_score)}
                  />
                )}
              </Stack>
              <Typography variant="body1" sx={{ fontWeight: 500, mb: 1 }}>
                {f.question}
              </Typography>
              <Stack direction="row" spacing={2} sx={{ flexWrap: "wrap" }}>
                <Typography variant="caption" color="text.secondary">
                  Resolves {formatDate(f.resolution_date)}
                </Typography>
                {!showResolved && (
                  <Typography variant="caption" color="text.secondary">
                    Last refreshed: {relativeFromNow(f.last_refreshed_at)}
                  </Typography>
                )}
                {showResolved && f.scored_probability !== null && (
                  <Typography variant="caption" color="text.secondary">
                    Time-weighted: {formatPercent(f.scored_probability, 1)}
                  </Typography>
                )}
              </Stack>
            </Box>
          </Stack>
        </CardContent>
      </CardActionArea>
    </Card>
  );
}
