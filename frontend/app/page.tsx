"use client";

import {
  Alert,
  Box,
  CircularProgress,
  Container,
  Stack,
  Typography,
} from "@mui/material";
import { useCallback, useEffect, useState } from "react";

import { EditQuestionDialog } from "../components/EditQuestionDialog";
import { QuestionCard } from "../components/QuestionCard";
import { SubmitForm } from "../components/SubmitForm";
import { questions as qApi, type QuestionRecord } from "../lib/api";

export default function HomePage() {
  const [items, setItems] = useState<QuestionRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<QuestionRecord | null>(null);

  const load = useCallback(async () => {
    try {
      const list = await qApi.list({ status: "pending", sort: "score", limit: 100 });
      setItems(list);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load questions");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Top-5: highest net_score among pending. Ties broken by created_at desc (server-side).
  const top5Ids = new Set((items ?? []).slice(0, 5).map((q) => q.id));

  return (
    <>
      <Container maxWidth="md" sx={{ py: 4, pb: 24 }}>
        <Box sx={{ mb: 4 }}>
          <Typography variant="h1" sx={{ mb: 1 }}>
            Submit & Vote
          </Typography>
          <Typography variant="body1" color="text.secondary">
            Submit a forecasting question, or upvote ones you want the agent to take on.
            The top 5 each month are auto-promoted for forecasting.
          </Typography>
        </Box>

        {error && <Alert severity="error" sx={{ mb: 2 }}>{error}</Alert>}

        {items === null ? (
          <Stack alignItems="center" sx={{ py: 6 }}>
            <CircularProgress />
          </Stack>
        ) : items.length === 0 ? (
          <Alert severity="info">
            No pending questions yet. Be the first to submit one below.
          </Alert>
        ) : (
          <Stack spacing={2}>
            {items.map((q) => (
              <QuestionCard
                key={q.id}
                question={q}
                isTop5={top5Ids.has(q.id)}
                isOwn={q.is_own}
                onChange={load}
                onEdit={(qq) => setEditing(qq)}
              />
            ))}
          </Stack>
        )}
      </Container>

      <SubmitForm onSubmitted={load} />

      <EditQuestionDialog
        question={editing}
        open={editing !== null}
        onClose={() => setEditing(null)}
        onSaved={load}
      />
    </>
  );
}
