"""The Chat page's Questions for you panel and the user's answer actions."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from app.models import Project
from app.project_routing import request_project
from app.questions import ANSWER_MAX_CHARS, QUESTION_ID, Question, QuestionStore, render_view
from app.security import validate_field
from app.storage import NotFoundError, ProjectStore


router = APIRouter()


def question_store(request: Request, project: Project) -> QuestionStore:
    return QuestionStore(
        ProjectStore(project).root,
        request.app.state.settings.auto_questions_view_limit,
    )


def questions_panel_context(request: Request, project: Project) -> dict:
    questions = question_store(request, project)
    book = questions.load()
    return {
        "project": project,
        "questions": sorted(book.questions, key=lambda item: item.number, reverse=True),
        "omitted": render_view(book, questions.view_limit).omitted,
        "open_count": sum(1 for item in book.questions if item.status == "open"),
        "cursor": book.next_id - 1,
        "answer_max": ANSWER_MAX_CHARS,
    }


def _question_id(value: str) -> str:
    if not QUESTION_ID.fullmatch(value):
        raise NotFoundError("question not found")
    return value


def _render(request: Request, name: str, context: dict) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request=request, name=name, context=context
    )


@router.get("/projects/{project_id}/questions", response_class=HTMLResponse)
async def questions_panel(request: Request, project_id: str) -> HTMLResponse:
    project = request_project(request, project_id)
    return _render(request, "_questions_panel.html", questions_panel_context(request, project))


@router.get("/projects/{project_id}/questions/new", response_class=HTMLResponse)
async def new_questions(
    request: Request,
    project_id: str,
    after: int = Query(..., ge=0),
) -> HTMLResponse:
    project = request_project(request, project_id)
    context = questions_panel_context(request, project)
    context["questions"] = [item for item in context["questions"] if item.number > after]
    return _render(request, "_questions_new.html", context)


async def _act(
    request: Request,
    project_id: str,
    question_id: str,
    action: Callable[[QuestionStore, str], Question | None],
) -> HTMLResponse:
    project = request_project(request, project_id)
    question_id = _question_id(question_id)
    questions = question_store(request, project)
    async with request.app.state.locks.project_lock(project.id):
        question = action(questions, question_id)
    context = questions_panel_context(request, project)
    if question is None:
        return _render(request, "_questions_summary_oob.html", context)
    return _render(request, "_question_item.html", {**context, "question": question, "oob_summary": True})


@router.post("/projects/{project_id}/questions/{question_id}/answer", response_class=HTMLResponse)
async def answer_question(
    request: Request, project_id: str, question_id: str, answer: str = Form(...)
) -> HTMLResponse:
    answer = validate_field(answer, "Answer", maximum=ANSWER_MAX_CHARS)
    return await _act(request, project_id, question_id, lambda store, qid: store.answer(qid, answer))


@router.post("/projects/{project_id}/questions/{question_id}/clear", response_class=HTMLResponse)
async def clear_answer(request: Request, project_id: str, question_id: str) -> HTMLResponse:
    return await _act(request, project_id, question_id, lambda store, qid: store.clear_answer(qid))


@router.post("/projects/{project_id}/questions/{question_id}/dismiss", response_class=HTMLResponse)
async def dismiss_question(request: Request, project_id: str, question_id: str) -> HTMLResponse:
    return await _act(request, project_id, question_id, lambda store, qid: store.dismiss(qid))


@router.post("/projects/{project_id}/questions/{question_id}/restore", response_class=HTMLResponse)
async def restore_question(request: Request, project_id: str, question_id: str) -> HTMLResponse:
    return await _act(request, project_id, question_id, lambda store, qid: store.restore(qid))


@router.post("/projects/{project_id}/questions/{question_id}/duplicate", response_class=HTMLResponse)
async def mark_duplicate(
    request: Request, project_id: str, question_id: str, duplicate_of: str = Form(...)
) -> HTMLResponse:
    original = validate_field(duplicate_of, "Duplicate of", maximum=16).strip()
    return await _act(
        request, project_id, question_id, lambda store, qid: store.mark_duplicate(qid, original)
    )


@router.post("/projects/{project_id}/questions/{question_id}/delete", response_class=HTMLResponse)
async def delete_question(request: Request, project_id: str, question_id: str) -> HTMLResponse:
    return await _act(request, project_id, question_id, lambda store, qid: store.delete(qid))
