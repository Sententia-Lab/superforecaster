"use client";

import {
  Alert,
  Box,
  Button,
  Container,
  Paper,
  Stack,
  TextField,
} from "@mui/material";
import { DatePicker } from "@mui/x-date-pickers/DatePicker";
import { useState } from "react";
import dayjs, { type Dayjs } from "dayjs";

import { questions as qApi, ApiError } from "../lib/api";

export function SubmitForm({ onSubmitted }: { onSubmitted: () => void }) {
  const [text, setText] = useState("");
  const [criteria, setCriteria] = useState("");
  const [date, setDate] = useState<Dayjs | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    setError(null);
    setSuccess(null);
    if (!text.trim() || !criteria.trim() || !date) {
      setError("All three fields are required.");
      return;
    }
    if (date.toDate().getTime() <= Date.now()) {
      setError("Resolution date must be in the future.");
      return;
    }
    setSubmitting(true);
    try {
      await qApi.create({
        text: text.trim(),
        resolution_criteria: criteria.trim(),
        proposed_resolution_date: date.toDate().toISOString(),
      });
      setSuccess("Submitted! It now appears in the list above.");
      setText("");
      setCriteria("");
      setDate(null);
      onSubmitted();
    } catch (e) {
      if (e instanceof ApiError && e.status === 429) {
        setError("You can only submit one question per 24 hours. Edit or delete your last submission instead.");
      } else {
        setError(e instanceof ApiError ? e.detail : "submission failed");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Paper
      elevation={3}
      sx={{
        position: "sticky",
        bottom: 0,
        mt: 4,
        borderTop: "1px solid",
        borderColor: "divider",
        py: 2,
        zIndex: 10,
      }}
    >
      <Container maxWidth="md">
        <Stack spacing={2}>
          {error && <Alert severity="error">{error}</Alert>}
          {success && <Alert severity="success">{success}</Alert>}
          <TextField
            label="Question"
            placeholder="Will the US enter a direct military conflict with Iran by end of 2026?"
            value={text}
            onChange={(e) => setText(e.target.value)}
            fullWidth
            size="small"
          />
          <TextField
            label="Resolution criteria"
            placeholder="Define exactly what counts as YES — e.g. 'US troops engage in combat operations on Iranian soil, confirmed by two independent news sources.'"
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            fullWidth
            multiline
            minRows={2}
            maxRows={4}
            size="small"
            helperText="Be specific. Vague criteria lead to ambiguous resolution."
          />
          <Stack direction={{ xs: "column", sm: "row" }} spacing={2}>
            <Box sx={{ flexGrow: 1 }}>
              <DatePicker
                label="Resolution date"
                value={date}
                onChange={(d) => setDate(d)}
                disablePast
                slotProps={{
                  textField: { fullWidth: true, size: "small" },
                }}
              />
            </Box>
            <Button
              variant="contained"
              onClick={handleSubmit}
              disabled={submitting}
              size="large"
            >
              {submitting ? "Submitting…" : "Submit"}
            </Button>
          </Stack>
        </Stack>
      </Container>
    </Paper>
  );
}
