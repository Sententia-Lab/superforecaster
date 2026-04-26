"use client";

import {
  Alert,
  CircularProgress,
  Container,
  Stack,
  Typography,
} from "@mui/material";
import { useEffect, useState } from "react";

import { ForecastSummaryCard } from "../../components/ForecastSummaryCard";
import { forecasts as fApi, type ForecastRecord } from "../../lib/api";

export default function PredictionsPage() {
  const [items, setItems] = useState<ForecastRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const list = await fApi.list("active", 100);
        if (!cancelled) setItems(list);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Sort by resolution_date ascending — soonest deadline first
  const sorted = items?.slice().sort(
    (a, b) =>
      new Date(a.resolution_date).getTime() - new Date(b.resolution_date).getTime()
  );

  return (
    <Container maxWidth="md" sx={{ py: 4 }}>
      <Typography variant="h1" sx={{ mb: 1 }}>
        In-Progress Predictions
      </Typography>
      <Typography variant="body1" color="text.secondary" sx={{ mb: 4 }}>
        Active forecasts ordered by soonest resolution. Click any to see the full
        decomposition, research, and update timeline.
      </Typography>

      {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

      {items === null ? (
        <Stack alignItems="center" sx={{ py: 6 }}>
          <CircularProgress />
        </Stack>
      ) : sorted!.length === 0 ? (
        <Alert severity="info">No active forecasts right now.</Alert>
      ) : (
        <Stack spacing={2}>
          {sorted!.map((f) => (
            <ForecastSummaryCard key={f.id} forecast={f} variant="active" />
          ))}
        </Stack>
      )}
    </Container>
  );
}
