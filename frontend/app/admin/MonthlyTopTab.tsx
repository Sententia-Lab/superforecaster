"use client";

import {
  Alert,
  Button,
  Chip,
  CircularProgress,
  Paper,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useState } from "react";

import { admin as aApi, questions as qApi, ApiError, type QuestionRecord } from "../../lib/api";

interface Props {
  notify: (msg: string, severity?: "success" | "error") => void;
}

export function MonthlyTopTab({ notify }: Props) {
  const [items, setItems] = useState<QuestionRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    try {
      const list = await qApi.topMonthly();
      setItems(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function runDigest() {
    setRunning(true);
    try {
      const promoted = await aApi.digestRun();
      notify(`Digest promoted ${promoted.length} question(s) to approved`);
      load();
    } catch (e) {
      notify(e instanceof ApiError ? e.detail : "digest failed", "error");
    } finally {
      setRunning(false);
    }
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (items === null) return <CircularProgress />;

  return (
    <>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 2 }}>
        <Typography variant="body2" color="text.secondary" sx={{ flexGrow: 1 }}>
          Top 5 voted questions submitted this calendar month. The monthly cron auto-promotes
          these to <code>approved</code> on the last day of the month — or click below to
          run it now.
        </Typography>
        <Button variant="contained" onClick={runDigest} disabled={running}>
          {running ? "Running…" : "Run digest now"}
        </Button>
      </Stack>

      {items.length === 0 ? (
        <Alert severity="info">No eligible questions this month yet.</Alert>
      ) : (
        <TableContainer component={Paper} variant="outlined">
          <Table>
            <TableHead>
              <TableRow>
                <TableCell>Question</TableCell>
                <TableCell align="center">Score</TableCell>
                <TableCell>Status</TableCell>
              </TableRow>
            </TableHead>
            <TableBody>
              {items.map((q) => (
                <TableRow key={q.id}>
                  <TableCell sx={{ fontWeight: 500 }}>{q.text}</TableCell>
                  <TableCell align="center">
                    {q.net_score > 0 ? `+${q.net_score}` : q.net_score}
                  </TableCell>
                  <TableCell>
                    <Chip
                      label={q.status}
                      size="small"
                      color={q.status === "approved" ? "success" : "default"}
                    />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </>
  );
}
