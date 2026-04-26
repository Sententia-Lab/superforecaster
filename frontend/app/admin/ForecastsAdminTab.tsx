"use client";

import {
  Alert,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogContentText,
  DialogTitle,
  FormControl,
  FormControlLabel,
  Paper,
  Radio,
  RadioGroup,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import {
  admin as aApi,
  forecasts as fApi,
  ApiError,
  type ForecastRecord,
} from "../../lib/api";
import { formatPercent, latestProbability, relativeFromNow } from "../../lib/utils";

interface Props {
  notify: (msg: string, severity?: "success" | "error") => void;
}

type ResolveOutcome = "yes" | "no" | "ambiguous";

export function ForecastsAdminTab({ notify }: Props) {
  const [items, setItems] = useState<ForecastRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState<ForecastRecord | null>(null);
  const [refreshing, setRefreshing] = useState<Set<string>>(new Set());
  const [globalRefreshing, setGlobalRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const list = await fApi.list(undefined, 100);
      setItems(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Pin flagged forecasts to the top
  const sorted = useMemo(() => {
    if (!items) return null;
    return items.slice().sort((a, b) => {
      if (a.flagged_for_resolution_review !== b.flagged_for_resolution_review) {
        return a.flagged_for_resolution_review ? -1 : 1;
      }
      return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
    });
  }, [items]);

  async function refreshOne(id: string) {
    setRefreshing((s) => new Set(s).add(id));
    try {
      const r = await fApi.refresh(id);
      notify(r.updated ? "New update written" : `No update — ${r.reason ?? "no change"}`);
      load();
    } catch (e) {
      notify(e instanceof ApiError ? e.detail : "refresh failed", "error");
    } finally {
      setRefreshing((s) => {
        const ns = new Set(s);
        ns.delete(id);
        return ns;
      });
    }
  }

  async function refreshAll() {
    setGlobalRefreshing(true);
    try {
      const summary = await aApi.refreshRun();
      notify(
        `Checked ${summary.total_checked}, updated ${summary.total_updated}, flagged ${summary.total_flagged_for_review}`
      );
      load();
    } catch (e) {
      notify(e instanceof ApiError ? e.detail : "refresh-run failed", "error");
    } finally {
      setGlobalRefreshing(false);
    }
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (sorted === null) return <CircularProgress />;
  if (sorted.length === 0) return <Alert severity="info">No forecasts yet.</Alert>;

  return (
    <>
      <Stack direction="row" justifyContent="flex-end" sx={{ mb: 2 }}>
        <Button variant="contained" disabled={globalRefreshing} onClick={refreshAll}>
          {globalRefreshing ? "Running…" : "Run all refreshes"}
        </Button>
      </Stack>

      <TableContainer component={Paper} variant="outlined">
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>Question</TableCell>
              <TableCell align="center">Probability</TableCell>
              <TableCell>Status</TableCell>
              <TableCell>Last refreshed</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {sorted.map((f) => {
              const isResolved = f.outcome !== null || f.is_ambiguous;
              const current = latestProbability(f.updates);
              return (
                <TableRow
                  key={f.id}
                  sx={
                    f.flagged_for_resolution_review
                      ? { bgcolor: "warning.50", "& td": { borderColor: "warning.main" } }
                      : undefined
                  }
                >
                  <TableCell>
                    {f.flagged_for_resolution_review && (
                      <Chip
                        label="Flagged: agent thinks resolved"
                        size="small"
                        color="warning"
                        sx={{ mb: 1 }}
                      />
                    )}
                    <Typography
                      component={Link}
                      href={`/forecasts/${f.id}`}
                      variant="body2"
                      sx={{ fontWeight: 500, color: "text.primary" }}
                    >
                      {f.question}
                    </Typography>
                  </TableCell>
                  <TableCell align="center">
                    {isResolved ? (
                      f.is_ambiguous ? (
                        <Chip label="ambig" size="small" />
                      ) : (
                        <Chip
                          label={f.outcome === 1.0 ? "YES" : "NO"}
                          color={f.outcome === 1.0 ? "success" : "error"}
                          size="small"
                        />
                      )
                    ) : (
                      formatPercent(current)
                    )}
                  </TableCell>
                  <TableCell>
                    {isResolved ? "resolved" : "active"}
                  </TableCell>
                  <TableCell sx={{ color: "text.secondary" }}>
                    {relativeFromNow(f.last_refreshed_at)}
                  </TableCell>
                  <TableCell align="right">
                    <Stack direction="row" spacing={1} justifyContent="flex-end">
                      {!isResolved && (
                        <Button
                          size="small"
                          disabled={refreshing.has(f.id)}
                          onClick={() => refreshOne(f.id)}
                        >
                          {refreshing.has(f.id) ? "…" : "Refresh"}
                        </Button>
                      )}
                      {!isResolved && (
                        <Button
                          size="small"
                          variant={f.flagged_for_resolution_review ? "contained" : "outlined"}
                          color={f.flagged_for_resolution_review ? "warning" : "primary"}
                          onClick={() => setResolving(f)}
                        >
                          Resolve
                        </Button>
                      )}
                    </Stack>
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </TableContainer>

      <ResolveDialog
        forecast={resolving}
        onClose={() => setResolving(null)}
        onResolved={() => {
          notify("Resolved");
          load();
        }}
      />
    </>
  );
}

function ResolveDialog({
  forecast,
  onClose,
  onResolved,
}: {
  forecast: ForecastRecord | null;
  onClose: () => void;
  onResolved: () => void;
}) {
  const [outcome, setOutcome] = useState<ResolveOutcome>("yes");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (forecast) {
      setOutcome("yes");
      setError(null);
    }
  }, [forecast]);

  if (!forecast) return null;

  async function submit() {
    if (!forecast) return;
    setSubmitting(true);
    setError(null);
    try {
      const value = outcome === "yes" ? 1.0 : outcome === "no" ? 0.0 : null;
      await fApi.resolve(forecast.id, value);
      onResolved();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "resolve failed");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Resolve forecast</DialogTitle>
      <DialogContent>
        <DialogContentText sx={{ mb: 2 }}>
          {forecast.question}
        </DialogContentText>
        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}
        <FormControl>
          <RadioGroup
            value={outcome}
            onChange={(e) => setOutcome(e.target.value as ResolveOutcome)}
          >
            <FormControlLabel value="yes" control={<Radio />} label="YES — the event occurred" />
            <FormControlLabel value="no" control={<Radio />} label="NO — the event did not occur" />
            <FormControlLabel
              value="ambiguous"
              control={<Radio />}
              label="Ambiguous — exclude from scoring"
            />
          </RadioGroup>
        </FormControl>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button onClick={submit} disabled={submitting} variant="contained" color="error">
          {submitting ? "Resolving…" : "Confirm"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
