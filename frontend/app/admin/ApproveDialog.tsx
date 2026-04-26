"use client";

import {
  Alert,
  Button,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Stack,
  TextField,
} from "@mui/material";
import { useEffect, useState } from "react";

import { questions as qApi, ApiError, type QuestionRecord } from "../../lib/api";

interface Props {
  question: QuestionRecord | null;
  onClose: () => void;
  onApproved: () => void;
}

export function ApproveDialog({ question, onClose, onApproved }: Props) {
  const [criteria, setCriteria] = useState("");
  const [date, setDate] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (question) {
      setCriteria(question.resolution_criteria);
      setDate(question.proposed_resolution_date.slice(0, 10));
      setError(null);
    }
  }, [question]);

  if (!question) return null;

  async function approve() {
    if (!question) return;
    try {
      const body: { resolution_date?: string; resolution_criteria?: string } = {};
      if (criteria !== question.resolution_criteria) body.resolution_criteria = criteria;
      if (date !== question.proposed_resolution_date.slice(0, 10)) {
        body.resolution_date = new Date(date).toISOString();
      }
      await qApi.approve(question.id, body);
      onApproved();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "approve failed");
    }
  }

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Approve question</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <Alert severity="info">
            Last chance to tighten the resolution criteria before the forecast runs.
          </Alert>
          <TextField
            label="Final resolution criteria"
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            fullWidth
            multiline
            minRows={3}
          />
          <TextField
            label="Final resolution date"
            type="date"
            value={date}
            onChange={(e) => setDate(e.target.value)}
            fullWidth
            InputLabelProps={{ shrink: true }}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button onClick={approve} variant="contained">Confirm approve</Button>
      </DialogActions>
    </Dialog>
  );
}
