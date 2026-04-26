"use client";

import {
  Alert,
  Button,
  CircularProgress,
  IconButton,
  Stack,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Paper,
  Typography,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  TextField,
} from "@mui/material";
import EditIcon from "@mui/icons-material/Edit";
import { useCallback, useEffect, useState } from "react";

import { questions as qApi, ApiError, type QuestionRecord } from "../../lib/api";
import { formatDate } from "../../lib/utils";
import { ApproveDialog } from "./ApproveDialog";

interface Props {
  notify: (msg: string, severity?: "success" | "error") => void;
}

export function PendingQuestionsTab({ notify }: Props) {
  const [items, setItems] = useState<QuestionRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<QuestionRecord | null>(null);
  const [approving, setApproving] = useState<QuestionRecord | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await qApi.list({ status: "pending", sort: "score", limit: 100 });
      setItems(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "load failed");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function reject(id: string) {
    if (!confirm("Reject this question?")) return;
    try {
      await qApi.reject(id);
      notify("Question rejected");
      load();
    } catch (e) {
      notify(e instanceof ApiError ? e.detail : "reject failed", "error");
    }
  }

  if (error) return <Alert severity="error">{error}</Alert>;
  if (items === null) return <CircularProgress />;
  if (items.length === 0) return <Alert severity="info">No pending questions.</Alert>;

  return (
    <>
      <TableContainer component={Paper} variant="outlined">
        <Table>
          <TableHead>
            <TableRow>
              <TableCell>Question</TableCell>
              <TableCell>Resolution criteria</TableCell>
              <TableCell align="center">Score</TableCell>
              <TableCell>Resolves</TableCell>
              <TableCell align="right">Actions</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {items.map((q) => (
              <TableRow key={q.id}>
                <TableCell sx={{ fontWeight: 500 }}>{q.text}</TableCell>
                <TableCell sx={{ maxWidth: 320 }}>
                  <Typography
                    variant="body2"
                    color="text.secondary"
                    sx={{
                      display: "-webkit-box",
                      WebkitLineClamp: 2,
                      WebkitBoxOrient: "vertical",
                      overflow: "hidden",
                    }}
                  >
                    {q.resolution_criteria}
                  </Typography>
                </TableCell>
                <TableCell align="center">{q.net_score > 0 ? `+${q.net_score}` : q.net_score}</TableCell>
                <TableCell>{formatDate(q.proposed_resolution_date)}</TableCell>
                <TableCell align="right">
                  <Stack direction="row" spacing={1} justifyContent="flex-end">
                    <IconButton size="small" onClick={() => setEditing(q)} aria-label="edit">
                      <EditIcon fontSize="small" />
                    </IconButton>
                    <Button size="small" variant="contained" onClick={() => setApproving(q)}>
                      Approve
                    </Button>
                    <Button size="small" color="error" onClick={() => reject(q.id)}>
                      Reject
                    </Button>
                  </Stack>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>

      <AdminEditDialog
        question={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          notify("Saved");
          load();
        }}
      />
      <ApproveDialog
        question={approving}
        onClose={() => setApproving(null)}
        onApproved={() => {
          notify("Approved");
          load();
        }}
      />
    </>
  );
}

function AdminEditDialog({
  question,
  onClose,
  onSaved,
}: {
  question: QuestionRecord | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [text, setText] = useState("");
  const [criteria, setCriteria] = useState("");
  const [date, setDate] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (question) {
      setText(question.text);
      setCriteria(question.resolution_criteria);
      setDate(question.proposed_resolution_date.slice(0, 10));
    }
  }, [question]);

  if (!question) return null;

  async function save() {
    if (!question) return;
    setError(null);
    try {
      const body: { text?: string; resolution_criteria?: string; proposed_resolution_date?: string } = {};
      if (text !== question.text) body.text = text;
      if (criteria !== question.resolution_criteria) body.resolution_criteria = criteria;
      if (date && date !== question.proposed_resolution_date.slice(0, 10)) {
        body.proposed_resolution_date = new Date(date).toISOString();
      }
      await qApi.edit(question.id, body);
      onSaved();
      onClose();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "save failed");
    }
  }

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>Edit question (admin)</DialogTitle>
      <DialogContent>
        <Stack spacing={2} sx={{ mt: 1 }}>
          {error && <Alert severity="error">{error}</Alert>}
          <TextField label="Question" value={text} onChange={(e) => setText(e.target.value)} fullWidth />
          <TextField
            label="Resolution criteria"
            value={criteria}
            onChange={(e) => setCriteria(e.target.value)}
            fullWidth
            multiline
            minRows={3}
          />
          <TextField
            label="Resolution date"
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
        <Button onClick={save} variant="contained">Save</Button>
      </DialogActions>
    </Dialog>
  );
}
