"use client";

import {
  Accordion,
  AccordionDetails,
  AccordionSummary,
  Alert,
  Box,
  Card,
  CardContent,
  Chip,
  CircularProgress,
  Container,
  Divider,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Tooltip,
  Typography,
} from "@mui/material";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import { use, useEffect, useState } from "react";

import { forecasts as fApi, type ForecastRecord } from "../../../lib/api";
import {
  brierColor,
  formatDate,
  formatDateTime,
  formatPercent,
  latestProbability,
} from "../../../lib/utils";

export default function ForecastDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const [forecast, setForecast] = useState<ForecastRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const f = await fApi.get(id);
        if (!cancelled) setForecast(f);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "load failed");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  if (error) {
    return (
      <Container maxWidth="md" sx={{ py: 4 }}>
        <Alert severity="error">{error}</Alert>
      </Container>
    );
  }

  if (forecast === null) {
    return (
      <Container maxWidth="md" sx={{ py: 4 }}>
        <Stack alignItems="center" sx={{ py: 6 }}>
          <CircularProgress />
        </Stack>
      </Container>
    );
  }

  const f = forecast;
  const current = latestProbability(f.updates);
  const isResolved = f.outcome !== null && !f.is_ambiguous;

  return (
    <Container maxWidth="md" sx={{ py: 4 }}>
      {/* Header */}
      <Stack direction="row" spacing={1} sx={{ mb: 1, flexWrap: "wrap" }}>
        <Chip label={f.category} size="small" variant="outlined" />
        {f.flagged_for_resolution_review && (
          <Chip label="Resolution flagged for admin review" size="small" color="warning" />
        )}
        {f.is_ambiguous && (
          <Chip label="Ambiguous — excluded from scoring" size="small" color="default" />
        )}
        {isResolved && (
          <Chip
            label={f.outcome === 1.0 ? "Resolved YES" : "Resolved NO"}
            size="small"
            color={f.outcome === 1.0 ? "success" : "error"}
          />
        )}
      </Stack>
      <Typography variant="h1" sx={{ mb: 2 }}>
        {f.question}
      </Typography>

      {/* Probability summary */}
      <Card variant="outlined" sx={{ mb: 3 }}>
        <CardContent>
          <Stack direction={{ xs: "column", sm: "row" }} spacing={4} alignItems="flex-start">
            <Box>
              <Typography variant="caption" color="text.secondary">
                {isResolved ? "TIME-WEIGHTED PROBABILITY" : "CURRENT PROBABILITY"}
              </Typography>
              <Typography variant="h2" color="primary" sx={{ fontSize: "3rem" }}>
                {formatPercent(isResolved ? f.scored_probability : current, 1)}
              </Typography>
              {!isResolved && f.updates.length > 0 && (
                <Typography variant="caption" color="text.secondary">
                  {f.updates[f.updates.length - 1].confidence} confidence
                </Typography>
              )}
            </Box>
            {isResolved && f.brier_score !== null && (
              <Box>
                <Typography variant="caption" color="text.secondary">
                  BRIER SCORE
                </Typography>
                <Tooltip title="Time-weighted average squared error vs. actual outcome. Lower is better.">
                  <Typography
                    variant="h2"
                    sx={{ fontSize: "3rem" }}
                    color={`${brierColor(f.brier_score)}.main`}
                  >
                    {f.brier_score.toFixed(3)}
                  </Typography>
                </Tooltip>
              </Box>
            )}
            <Box sx={{ flexGrow: 1 }}>
              <Stack spacing={0.5}>
                <Row label="Resolves" value={formatDate(f.resolution_date)} />
                <Row label="Submission deadline" value={formatDate(f.submission_deadline)} />
                {f.last_refreshed_at && (
                  <Row label="Last refreshed" value={formatDateTime(f.last_refreshed_at)} />
                )}
                {f.resolved_at && (
                  <Row label="Resolved at" value={formatDateTime(f.resolved_at)} />
                )}
              </Stack>
            </Box>
          </Stack>
        </CardContent>
      </Card>

      {f.is_ambiguous && (
        <Alert severity="info" sx={{ mb: 3 }}>
          This forecast was resolved as ambiguous. The criteria did not cleanly resolve, so it
          is excluded from the calibration aggregate.
        </Alert>
      )}

      {/* Resolution criteria */}
      <Section title="Resolution criteria">
        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
          {f.resolution_criteria}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: "block" }}>
          Source: {f.resolution_source}
        </Typography>
      </Section>

      {/* Update timeline */}
      <Section title={`Update timeline (${f.updates.length})`}>
        <Stack spacing={2}>
          {f.updates.map((u, i) => (
            <Box key={u.id} sx={{ display: "flex", gap: 2 }}>
              <Box sx={{ minWidth: 60, textAlign: "right" }}>
                <Typography variant="h3" color="primary">
                  {formatPercent(u.probability, 0)}
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  {u.confidence}
                </Typography>
              </Box>
              <Box sx={{ flexGrow: 1, borderLeft: "2px solid", borderColor: "divider", pl: 2 }}>
                <Stack direction="row" spacing={1} sx={{ mb: 0.5, flexWrap: "wrap" }}>
                  <Typography variant="caption" color="text.secondary">
                    {formatDateTime(u.created_at)}
                  </Typography>
                  {i === 0 && <Chip label="initial" size="small" />}
                  {u.is_late && <Chip label="late" size="small" color="warning" />}
                </Stack>
                <Typography variant="body2">{u.reasoning}</Typography>
              </Box>
            </Box>
          ))}
        </Stack>
      </Section>

      {/* Research panel */}
      <Section title="Research">
        {f.research.empirical_base_rate !== null && (
          <Box sx={{ mb: 2 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
              EMPIRICAL BASE RATE (from historical analogs)
            </Typography>
            <Typography variant="h2" color="primary">
              {formatPercent(f.research.empirical_base_rate, 1)}
            </Typography>
            {f.research.base_rate_note && (
              <Typography variant="caption" color="text.secondary">
                {f.research.base_rate_note}
              </Typography>
            )}
          </Box>
        )}

        {f.research.historical_analogs.length > 0 && (
          <>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
              HISTORICAL ANALOGS
            </Typography>
            <TableContainer component={Paper} variant="outlined" sx={{ mb: 2 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Event</TableCell>
                    <TableCell align="center">Outcome</TableCell>
                    <TableCell>Relevance</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {f.research.historical_analogs.map((a, i) => (
                    <TableRow key={i}>
                      <TableCell>{a.description}</TableCell>
                      <TableCell align="center">
                        <Chip
                          label={a.outcome === 1.0 ? "YES" : "NO"}
                          size="small"
                          color={a.outcome === 1.0 ? "success" : "error"}
                          variant="outlined"
                        />
                      </TableCell>
                      <TableCell sx={{ color: "text.secondary" }}>{a.relevance}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </>
        )}

        {f.research.causal_forces.length > 0 && (
          <Box sx={{ mb: 2 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
              CAUSAL FORCES
            </Typography>
            <Stack spacing={0.5}>
              {f.research.causal_forces.map((c, i) => (
                <Typography key={i} variant="body2">• {c}</Typography>
              ))}
            </Stack>
          </Box>
        )}

        <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
          <EvidenceList title="Supporting" items={f.research.evidence.supporting} positive />
          <EvidenceList title="Contradicting" items={f.research.evidence.contradicting} positive={false} />
        </Stack>

        {f.research.uncertainties.length > 0 && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
              KEY UNCERTAINTIES
            </Typography>
            <Stack spacing={0.5}>
              {f.research.uncertainties.map((u, i) => (
                <Typography key={i} variant="body2" color="text.secondary">• {u}</Typography>
              ))}
            </Stack>
          </Box>
        )}
      </Section>

      {/* Decomposition */}
      <Section title="Decomposition">
        <Stack spacing={1}>
          {f.decompositions.map((d, i) => (
            <Accordion key={i} disableGutters variant="outlined">
              <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                <Stack direction="row" spacing={2} sx={{ width: "100%", flexWrap: "wrap" }}>
                  <Typography variant="body2" sx={{ fontWeight: 500, flexGrow: 1 }}>
                    {d.question}
                  </Typography>
                  <Chip label={`${formatPercent(d.probability, 0)} · ${d.confidence}`} size="small" />
                </Stack>
              </AccordionSummary>
              <AccordionDetails>
                <Typography variant="body2" color="text.secondary">
                  {d.rationale}
                </Typography>
              </AccordionDetails>
            </Accordion>
          ))}
        </Stack>
      </Section>

      {/* Initial reasoning */}
      <Section title="Initial reasoning">
        <Typography variant="body2" sx={{ whiteSpace: "pre-wrap" }}>
          {f.initial_reasoning}
        </Typography>
      </Section>
    </Container>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <Stack direction="row" spacing={2}>
      <Typography variant="caption" color="text.secondary" sx={{ minWidth: 140 }}>
        {label}
      </Typography>
      <Typography variant="body2">{value}</Typography>
    </Stack>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <Box sx={{ mb: 4 }}>
      <Typography variant="h2" sx={{ mb: 1.5 }}>
        {title}
      </Typography>
      <Divider sx={{ mb: 2 }} />
      {children}
    </Box>
  );
}

function EvidenceList({
  title,
  items,
  positive,
}: {
  title: string;
  items: string[];
  positive: boolean;
}) {
  if (items.length === 0) {
    return (
      <Box sx={{ flex: 1 }}>
        <Typography variant="caption" color="text.secondary">
          {title.toUpperCase()}
        </Typography>
        <Typography variant="body2" color="text.disabled">
          (none recorded)
        </Typography>
      </Box>
    );
  }
  return (
    <Box sx={{ flex: 1 }}>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
        {title.toUpperCase()}
      </Typography>
      <Stack spacing={0.5}>
        {items.map((e, i) => (
          <Typography
            key={i}
            variant="body2"
            sx={{
              borderLeft: 3,
              borderColor: positive ? "success.main" : "error.main",
              pl: 1.5,
            }}
          >
            {e}
          </Typography>
        ))}
      </Stack>
    </Box>
  );
}
