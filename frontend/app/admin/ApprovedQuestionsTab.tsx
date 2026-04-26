"use client";

import {
  Alert,
  Button,
  CircularProgress,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useState } from "react";

import { questions as qApi, ApiError, type QuestionRecord } from "../../lib/api";
import { formatDate } from "../../lib/utils";

interface Props {
  notify: (msg: string, severity?: "success" | "error") => void;
}

export function ApprovedQuestionsTab({ notify }: Props) {
  const [items, setItems] = useState<QuestionRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState<Set<string>>(new Set());

  const load = useCallback(async () => {
    try {
      const list = await qApi.list({ status: "approved", sort: "score", limit: 100 });
      setItems(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function runForecast(id: string) {
    setRunning((s) => new Set(s).add(id));
    try {
      await qApi.forecast(id);
      notify("Forecast started — agent is running");
      load();
    } catch (e) {
      notify(e instanceof ApiError ? e.detail : "forecast failed", "error");
    } finally {
      setRunning((s) => {
        const ns = new Set(s);
        ns.delete(id);
        return ns;
      });
    }
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (items === null) return <CircularProgress />;
  if (items.length === 0) return <Alert severity="info">No approved questions waiting to forecast.</Alert>;

  return (
    <TableContainer component={Paper} variant="outlined">
      <Table>
        <TableHead>
          <TableRow>
            <TableCell>Question</TableCell>
            <TableCell>Resolution criteria</TableCell>
            <TableCell>Resolves</TableCell>
            <TableCell align="right">Action</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {items.map((q) => (
            <TableRow key={q.id}>
              <TableCell sx={{ fontWeight: 500 }}>{q.text}</TableCell>
              <TableCell sx={{ maxWidth: 400 }}>
                <Typography variant="body2" color="text.secondary">
                  {q.resolution_criteria}
                </Typography>
              </TableCell>
              <TableCell>{formatDate(q.proposed_resolution_date)}</TableCell>
              <TableCell align="right">
                <Button
                  size="small"
                  variant="contained"
                  disabled={running.has(q.id)}
                  onClick={() => runForecast(q.id)}
                >
                  {running.has(q.id) ? "Running…" : "Run forecast"}
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}
