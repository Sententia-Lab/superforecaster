"use client";

import {
  Box,
  Card,
  CardContent,
  Chip,
  Collapse,
  IconButton,
  Stack,
  Tooltip,
  Typography,
} from "@mui/material";
import ThumbUpIcon from "@mui/icons-material/ThumbUp";
import ThumbDownIcon from "@mui/icons-material/ThumbDown";
import StarIcon from "@mui/icons-material/Star";
import EditIcon from "@mui/icons-material/Edit";
import DeleteIcon from "@mui/icons-material/Delete";
import { useState } from "react";

import type { QuestionRecord } from "../lib/api";
import { questions as qApi, ApiError } from "../lib/api";
import { formatDate } from "../lib/utils";

interface Props {
  question: QuestionRecord;
  isTop5: boolean;
  /** True if the caller (by IP) submitted this question — show edit/delete. */
  isOwn: boolean;
  onChange: () => void;
  onEdit: (q: QuestionRecord) => void;
}

export function QuestionCard({ question, isTop5, isOwn, onChange, onEdit }: Props) {
  const [expanded, setExpanded] = useState(false);
  const [voting, setVoting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const score = question.net_score;
  const userVote = question.user_vote;

  async function handleVote(value: 1 | -1) {
    setVoting(true);
    setError(null);
    try {
      if (userVote === value) {
        await qApi.unvote(question.id);
      } else {
        await qApi.vote(question.id, value);
      }
      onChange();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "vote failed");
    } finally {
      setVoting(false);
    }
  }

  async function handleDelete() {
    if (!confirm("Delete this submission?")) return;
    try {
      await qApi.delete(question.id);
      onChange();
    } catch (e) {
      setError(e instanceof ApiError ? e.detail : "delete failed");
    }
  }

  return (
    <Card
      variant="outlined"
      sx={{
        borderColor: isTop5 ? "primary.main" : "divider",
        borderWidth: isTop5 ? 2 : 1,
        cursor: "pointer",
      }}
      onClick={() => setExpanded((v) => !v)}
    >
      <CardContent>
        <Stack direction="row" spacing={2} alignItems="flex-start">
          <Stack
            alignItems="center"
            spacing={0.5}
            sx={{ minWidth: 56 }}
            onClick={(e) => e.stopPropagation()}
          >
            <IconButton
              size="small"
              color={userVote === 1 ? "primary" : "default"}
              onClick={() => handleVote(1)}
              disabled={voting}
              aria-label="upvote"
            >
              <ThumbUpIcon fontSize="small" />
            </IconButton>
            <Typography variant="h3" sx={{ fontWeight: 700 }}>
              {score > 0 ? `+${score}` : score}
            </Typography>
            <IconButton
              size="small"
              color={userVote === -1 ? "error" : "default"}
              onClick={() => handleVote(-1)}
              disabled={voting}
              aria-label="downvote"
            >
              <ThumbDownIcon fontSize="small" />
            </IconButton>
          </Stack>
          <Box sx={{ flexGrow: 1, minWidth: 0 }}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1, flexWrap: "wrap" }}>
              {isTop5 && (
                <Tooltip title="Top 5 — eligible for next batch">
                  <Chip
                    icon={<StarIcon />}
                    label="Top 5"
                    color="primary"
                    size="small"
                  />
                </Tooltip>
              )}
              <Chip
                label={question.status}
                size="small"
                variant="outlined"
                color={
                  question.status === "approved"
                    ? "success"
                    : question.status === "forecasted"
                    ? "info"
                    : question.status === "rejected"
                    ? "error"
                    : "default"
                }
              />
              <Typography variant="caption" color="text.secondary">
                resolves {formatDate(question.proposed_resolution_date)}
              </Typography>
            </Stack>
            <Typography variant="body1" sx={{ fontWeight: 500 }}>
              {question.text}
            </Typography>
            <Collapse in={expanded}>
              <Box sx={{ mt: 1.5, pt: 1.5, borderTop: "1px solid", borderColor: "divider" }}>
                <Typography variant="caption" color="text.secondary" sx={{ fontWeight: 600 }}>
                  RESOLUTION CRITERIA
                </Typography>
                <Typography variant="body2" sx={{ mt: 0.5 }}>
                  {question.resolution_criteria}
                </Typography>
                {isOwn && question.status === "pending" && (
                  <Stack
                    direction="row"
                    spacing={1}
                    sx={{ mt: 1.5 }}
                    onClick={(e) => e.stopPropagation()}
                  >
                    <IconButton size="small" onClick={() => onEdit(question)} aria-label="edit">
                      <EditIcon fontSize="small" />
                    </IconButton>
                    <IconButton size="small" onClick={handleDelete} aria-label="delete">
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Stack>
                )}
              </Box>
            </Collapse>
            {error && (
              <Typography variant="caption" color="error" sx={{ mt: 1, display: "block" }}>
                {error}
              </Typography>
            )}
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}
