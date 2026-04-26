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

import { questions as qApi, ApiError, type QuestionRecord } from "../lib/api";

interface Props {
  question: QuestionRecord | null;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}

export function EditQuestionDialog({ question, open, onClose, onSaved }: Props) {
  const [text, setText] = useState("");
  const [criteria, setCriteria] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (question) {
      setText(question.text);
      setCriteria(question.resolution_criteria);
      setError(null);
    }
  }, [question]);

  async function handleSave() {
    if (!question) return;
    setSaving(true);
    setError(null);
    try {
      await qApi.edit(question.id, {
        text: text.trim(),
        resolution_criteria: criteria.trim(),
      });
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "save failed");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Edit submission</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <TextField
            label="Question"
            value={text}
            onChange={(e) => setText(e.target.value)}
            fullWidth
          />
          <TextField
            label="Resolution criteria"
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            fullWidth
            multiline
            minRows={3}
          />
        </Stack>
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
        <Button onClick={handleSave} disabled={saving} variant="contained">
          {saving ? "Saving…" : "Save"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
