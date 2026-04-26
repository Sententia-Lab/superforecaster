"use client";

import {
  Alert,
  Box,
  CircularProgress,
  Container,
  Paper,
  Stack,
  Typography,
} from "@mui/material";
import { useEffect, useState } from "react";

import { ForecastSummaryCard } from "../../components/ForecastSummaryCard";
import {
  calibration as cApi,
  forecasts as fApi,
  type CalibrationReport,
  type ForecastRecord,
} from "../../lib/api";
import { brierColor } from "../../lib/utils";

export default function ResolvedPage() {
  const [items, setItems] = useState<ForecastRecord[] | null>(null);
  const [report, setReport] = useState<CalibrationReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [list, rep] = await Promise.all([
          fApi.list("resolved", 100),
          cApi.get(),
        ]);
        if (!cancelled) {
          setItems(list);
          setReport(rep);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Sort by resolved_at desc
  const sorted = items?.slice().sort((a, b) => {
    const ta = a.resolved_at ? new Date(a.resolved_at).getTime() : 0;
    const tb = b.resolved_at ? new Date(b.resolved_at).getTime() : 0;
    return tb - ta;
  });

  return (
    <Container maxWidth="md" sx={{ py: 4 }}>
      <Typography variant="h1" sx={{ mb: 1 }}>
        Resolved Predictions
      </Typography>
      <Typography variant="body1" color="text.secondary" sx={{ mb: 4 }}>
        Closed forecasts with their actual outcomes and time-weighted Brier scores.
        Lower Brier is better — calibrated forecasters score 0.10–0.15.
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {report && report.total_resolved > 0 && (
        <Paper variant="outlined" sx={{ p: 3, mb: 4 }}>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={4}>
            <Box>
              <Typography variant="caption" color="text.secondary">
                AGGREGATE BRIER SCORE
              </Typography>
              <Typography
                variant="h2"
                color={
                  report.aggregate_brier_score !== null
                    ? `${brierColor(report.aggregate_brier_score)}.main`
                    : "text.primary"
                }
              >
                {report.aggregate_brier_score?.toFixed(3) ?? "—"}
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">
                RESOLVED FORECASTS
              </Typography>
              <Typography variant="h2">{report.total_resolved}</Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">
                AMBIGUOUS (EXCLUDED)
              </Typography>
              <Typography variant="h2">{report.total_ambiguous_excluded}</Typography>
            </Box>
          </Stack>
          {report.buckets.length > 0 && (
            <Box sx={{ mt: 3 }}>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                CALIBRATION BY PROBABILITY BUCKET
              </Typography>
              <Stack spacing={1}>
                {report.buckets.map((b) => (
                  <Box key={b.range}>
                    <Stack direction="row" justifyContent="space-between">
                      <Typography variant="body2">{b.range}</Typography>
                      <Typography variant="body2" color="text.secondary">
                        predicted {(b.predicted_avg * 100).toFixed(0)}% → actual{" "}
                        {(b.actual_frequency * 100).toFixed(0)}% (n={b.count})
                      </Typography>
                    </Stack>
                    <Box
                      sx={{
                        position: "relative",
                        height: 6,
                        bgcolor: "action.hover",
                        borderRadius: 3,
                        overflow: "hidden",
                      }}
                    >
                      <Box
                        sx={{
                          position: "absolute",
                          left: 0,
                          top: 0,
                          height: "100%",
                          width: `${b.actual_frequency * 100}%`,
                          bgcolor: "primary.main",
                        }}
                      />
                      <Box
                        sx={{
                          position: "absolute",
                          left: `${b.predicted_avg * 100}%`,
                          top: -2,
                          width: 2,
                          height: 10,
                          bgcolor: "secondary.main",
                        }}
                      />
                    </Box>
                  </Box>
                ))}
              </Stack>
            </Box>
          )}
        </Paper>
      )}

      {items === null ? (
        <Stack alignItems="center" sx={{ py: 6 }}>
          <CircularProgress />
        </Stack>
      ) : sorted!.length === 0 ? (
        <Alert severity="info">No resolved forecasts yet.</Alert>
      ) : (
        <Stack spacing={2}>
          {sorted!.map((f) => (
            <ForecastSummaryCard key={f.id} forecast={f} variant="resolved" />
          ))}
        </Stack>
      )}
    </Container>
  );
}
