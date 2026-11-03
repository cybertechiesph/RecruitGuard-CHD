"""Rules that keep the code's structure true: the services package layout, the
one door for case movements, Manila dates and the workflow map."""

import ast
import re
import symtable
from pathlib import Path
from types import SimpleNamespace

from django.test import SimpleTestCase

from ..models import PositionPosting, RecruitmentCase, RecruitmentUser


class ServicesPackageArchitectureTests(SimpleTestCase):
    """F15: rules that keep recruitment/services/ split by stage and importable."""

    package_dir = Path(__file__).resolve().parent.parent / "services"

    def _modules(self):
        for path in sorted(self.package_dir.glob("*.py")):
            if path.name != "__init__.py":
                yield path.stem, ast.parse(path.read_text(encoding="utf-8"))

    def _siblings_imported(self, tree):
        return {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None
            for alias in node.names
        }

    def test_every_public_name_is_still_importable_from_the_package(self):
        from recruitment import services

        names = (self.package_dir / "public_names.txt").read_text().split()
        self.assertGreaterEqual(len(names), 503)
        missing = [name for name in names if not hasattr(services, name)]
        self.assertEqual(missing, [], "re-export these from recruitment/services/__init__.py")

    def test_modules_call_siblings_through_the_module_object(self):
        for name, tree in self._modules():
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level == 1:
                    with self.subTest(module=name, line=node.lineno):
                        self.assertIsNone(
                            node.module,
                            f"services/{name}.py: use `from . import {node.module}` and "
                            f"call `{node.module}.<name>(...)`, not `from .{node.module} import`",
                        )

    def test_leaf_modules_import_nothing_above_them(self):
        allowed = {"common": set(), "clock": {"common"}, "workflow_map": set()}
        for name, tree in self._modules():
            if name in allowed:
                with self.subTest(module=name):
                    self.assertLessEqual(self._siblings_imported(tree), allowed[name])

    def test_no_local_variable_hides_a_sibling_module(self):
        for name, tree in self._modules():
            siblings = self._siblings_imported(tree)
            source = (self.package_dir / f"{name}.py").read_text(encoding="utf-8")
            pending = list(symtable.symtable(source, f"{name}.py", "exec").get_children())
            while pending:
                table = pending.pop()
                pending.extend(table.get_children())
                hidden = {
                    symbol.get_name()
                    for symbol in table.get_symbols()
                    if (symbol.is_local() or symbol.is_parameter())
                    and symbol.get_name() in siblings
                }
                with self.subTest(module=name, scope=table.get_name()):
                    self.assertEqual(hidden, set(), "rename the local; it hides a sibling module")

    def test_only_transition_case_moves_a_case(self):
        """F15 round 2: every change of a case's stage, status, handler or lock goes through
        services.routing.transition_case, so each movement is locked and recorded alike."""
        guarded = {"current_stage", "case_status", "current_handler_role", "is_stage_locked", "locked_stage"}
        allowed = {
            ("routing", "transition_case"),
            ("routing", "_transition_case_stage"),
            # Birth, not a movement: the application is routed before its case exists.
            ("intake", "submit_application"),
        }
        app_dir = self.package_dir.parent
        for path in sorted(app_dir.rglob("*.py")):
            relative = path.relative_to(app_dir)
            if relative.parts[0] in ("migrations", "tests"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for function in ast.walk(tree):
                if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if (path.stem, function.name) in allowed:
                    continue
                for node in ast.walk(function):
                    targets = []
                    if isinstance(node, ast.Assign):
                        targets = node.targets
                    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                        targets = [node.target]
                    for target in targets:
                        for part in ast.walk(target):
                            if isinstance(part, ast.Attribute) and part.attr in guarded:
                                self.fail(
                                    f"{relative}:{node.lineno} {function.name} sets "
                                    f".{part.attr}; call routing.transition_case instead"
                                )
                    if (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "update"
                        and guarded & {keyword.arg for keyword in node.keywords}
                    ):
                        self.fail(f"{relative}:{node.lineno} {function.name} bulk-updates a case field")

    def test_dates_come_from_manila_time(self):
        """F15 round 4 (keeps F3 fixed): a bare `.date()` on an aware date-time gives the UTC
        date, a day early before 8 a.m. Manila; a naive now()/today() uses the server clock.
        Convert with services.clock.local_day, timezone.localdate() or timezone.localtime()."""
        naive = {("datetime", "now"), ("datetime", "today"), ("datetime", "utcnow"), ("date", "today")}
        app_dir = self.package_dir.parent
        clock_path = self.package_dir / "clock.py"
        for path in sorted(app_dir.rglob("*.py")):
            relative = path.relative_to(app_dir)
            if relative.parts[0] in ("migrations", "tests"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                receiver = ast.unparse(node.func.value).split(".")[-1]
                with self.subTest(file=str(relative), line=node.lineno):
                    if node.func.attr == "date" and not node.args and path != clock_path:
                        self.fail(f"{relative}:{node.lineno} calls .date(); use clock.local_day()")
                    if (receiver, node.func.attr) in naive:
                        self.fail(f"{relative}:{node.lineno} uses naive {receiver}.{node.func.attr}()")


class WorkflowMapTests(SimpleTestCase):
    """F15 round 3: services/workflow_map.py is the one description of the workflow; the
    progress bar, the case page's sections, routing owners and the docs all read from it."""

    def _application(self, branch, level, stage=""):
        return SimpleNamespace(
            branch=branch,
            level=level,
            position=SimpleNamespace(level=level),
            case=SimpleNamespace(current_stage=stage),
            status="",
        )

    def _paths(self):
        from recruitment.services import workflow_map

        return workflow_map, workflow_map.STEPS.items()

    def test_every_path_starts_with_its_level_owner_and_ends_in_completion(self):
        workflow_map, paths = self._paths()
        owners = {
            PositionPosting.Level.LEVEL_1: RecruitmentUser.Role.SECRETARIAT,
            PositionPosting.Level.LEVEL_2: RecruitmentUser.Role.HRM_CHIEF,
        }
        for (branch, level), steps in paths:
            with self.subTest(branch=branch, level=level):
                self.assertEqual(workflow_map.stage_owner(steps[0].stage, level), owners[level])
                self.assertEqual(steps[-1].stage, RecruitmentCase.Stage.COMPLETION)
                self.assertEqual(workflow_map.stage_owner(steps[-1].stage, level), owners[level])
                stages = [step.stage for step in steps]
                # A case never comes back to a stage it has left.
                self.assertEqual(stages, sorted(stages, key=stages.index))
                self.assertEqual(len({step.key for step in steps}), len(steps))

    def test_progress_bar_reads_the_map(self):
        from recruitment.templatetags.recruitment_ui import _case_step_for_stage, _case_step_keys

        workflow_map, paths = self._paths()
        for (branch, level), steps in paths:
            application = self._application(branch, level)
            with self.subTest(branch=branch, level=level):
                self.assertEqual(
                    _case_step_keys(application), [(step.key, step.label) for step in steps]
                )
                for step in steps:
                    for section in step.sections:
                        self.assertEqual(
                            _case_step_for_stage(application, step.stage, section), step.key
                        )
                self.assertEqual(
                    _case_step_for_stage(application, RecruitmentCase.Stage.CLOSED, ""),
                    "completion",
                )

    def test_case_page_sections_follow_the_map(self):
        from recruitment.services import routing

        workflow_map, paths = self._paths()
        for (branch, level), steps in paths:
            for stage in {step.stage for step in steps}:
                expected = [s for step in steps if step.stage == stage for s in step.sections]
                with self.subTest(branch=branch, level=level, stage=stage):
                    sequence = routing._workflow_detail_sequence(
                        self._application(branch, level, stage)
                    )
                    self.assertEqual([s for s in sequence if s in expected], expected)

    def test_routing_owners_and_statuses_come_from_the_map(self):
        from recruitment.services import intake, routing

        workflow_map, paths = self._paths()
        for (branch, level), steps in paths:
            application = self._application(branch, level)
            with self.subTest(branch=branch, level=level):
                intake_stage = steps[0].stage
                self.assertEqual(
                    routing.level_owner_role(application),
                    workflow_map.stage_owner(intake_stage, level),
                )
                self.assertEqual(
                    intake._route_for_submission(application),
                    (
                        workflow_map.stage_owner(intake_stage, level),
                        workflow_map.status_for_stage(intake_stage),
                    ),
                )
                for step in steps:
                    self.assertEqual(
                        routing._handler_role_from_stage(step.stage, application=application),
                        workflow_map.stage_owner(step.stage, level),
                    )
                    status = routing._application_status_from_stage(step.stage)
                    if step.stage != RecruitmentCase.Stage.COMPLETION:
                        self.assertEqual(routing._review_stage_from_application_status(status), step.stage)

    def test_role_tables_derived_from_the_map_keep_the_office_rules(self):
        from recruitment.services import access, interview, selection

        Stage, Role = RecruitmentCase.Stage, RecruitmentUser.Role
        self.assertEqual(
            interview.INTERVIEW_RATING_ROLES_BY_STAGE,
            {Stage.HRM_CHIEF_REVIEW: {Role.HRM_CHIEF}, Stage.HRMPSB_REVIEW: {Role.HRMPSB_MEMBER}},
        )
        self.assertEqual(
            selection.DELIBERATION_ROLES_BY_BRANCH,
            {PositionPosting.Branch.COS: {Role.HRM_CHIEF}, PositionPosting.Branch.PLANTILLA: {Role.HRMPSB_MEMBER}},
        )
        self.assertEqual(
            access.LEVEL_TWO_FOLLOW_STAGES,
            {Stage.HRMPSB_REVIEW, Stage.APPOINTING_AUTHORITY_REVIEW},
        )

    def test_docs_table_matches_the_code(self):
        from recruitment.management.commands.print_workflow_map import (
            DOC_PATH,
            END_MARKER,
            START_MARKER,
            render_workflow_map,
        )

        text = DOC_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")
        block = text.split(START_MARKER, 1)[1].split(END_MARKER, 1)[0]
        self.assertEqual(
            block.strip(),
            render_workflow_map().strip(),
            "Run `python manage.py print_workflow_map --write` and commit docs/workflow-rules.md.",
        )


class ViewsPackageArchitectureTests(SimpleTestCase):
    """F15 round 6: recruitment/views/ is split by area. Modules lean only on `common` and the
    case page in `case`, which keeps the package free of import cycles. Round H: the case page
    is built from `case_shared` and one `case_<step>` module per step, which lean only on
    `common`."""

    package_dir = Path(__file__).resolve().parent.parent / "views"
    case_parts = {
        "case_shared",
        "case_screening",
        "case_exam",
        "case_interview",
        "case_car",
        "case_decision",
        "case_completion",
    }
    allowed = {"common": set(), "case": {"common"} | case_parts}

    def _allowed_for(self, module):
        if module in self.case_parts:
            return {"common"}
        return self.allowed.get(module, {"common", "case"})

    def _sibling_imports(self, tree):
        siblings = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 1:
                if node.module:
                    siblings.add(node.module)
                else:
                    # `from . import case_shared` names the sibling in the alias.
                    siblings.update(alias.name for alias in node.names)
        return siblings

    def test_every_case_module_is_listed(self):
        on_disk = {path.stem for path in self.package_dir.glob("case_*.py")}
        self.assertEqual(on_disk, self.case_parts)

    def test_every_public_name_is_still_importable_from_the_package(self):
        from recruitment import views

        names = (self.package_dir / "public_names.txt").read_text().split()
        missing = [name for name in names if not hasattr(views, name)]
        self.assertEqual(missing, [], "re-export these from recruitment/views/__init__.py")

    def test_modules_import_only_common_and_case(self):
        for path in sorted(self.package_dir.glob("*.py")):
            if path.name == "__init__.py":
                continue
            siblings = self._sibling_imports(ast.parse(path.read_text(encoding="utf-8")))
            with self.subTest(module=path.stem):
                self.assertLessEqual(siblings, self._allowed_for(path.stem))


class BrandingGuardTests(SimpleTestCase):
    """Round I1: "RecruitGuard" is the study's title only. Every user-facing surface says
    "DOH–CHD CALABARZON Recruitment Portal", so no readable template text, form attribute or
    non-docstring Python string may carry the study name or its "RG" short form. Script and
    style blocks, template comments and CSS class names (``rg-…``) are code, not text."""

    root = Path(__file__).resolve().parent.parent.parent
    pattern = re.compile(r"RecruitGuard|\bRG\b")
    # Machine values nobody reads: the X-Requested-With header recruitguard-autosave.js sends.
    technical_values = {"RG-Autosave"}

    def _template_text(self, source):
        source = re.sub(r"<(script|style)\b.*?</\1>", " ", source, flags=re.S | re.I)
        source = re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", " ", source, flags=re.S)
        source = re.sub(r"{#.*?#}|<!--.*?-->", " ", source, flags=re.S)
        attributes = re.findall(r'\b(?:placeholder|title|aria-label|alt|value)="([^"]*)"', source)
        text = re.sub(r"{%.*?%}|{{.*?}}", " ", source, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return [text, *attributes]

    def test_templates_do_not_show_the_study_name(self):
        offenders = []
        for folder in ("templates", "recruitment/templates"):
            for path in sorted((self.root / folder).rglob("*")):
                if path.suffix not in {".html", ".txt"}:
                    continue
                for chunk in self._template_text(path.read_text(encoding="utf-8")):
                    for match in self.pattern.finditer(chunk):
                        offenders.append(f"{path.relative_to(self.root)}: …{chunk[max(0, match.start() - 30):match.end() + 30].strip()}…")
        self.assertEqual(offenders, [])

    def test_python_strings_do_not_show_the_study_name(self):
        offenders = []
        for path in sorted((self.root / "recruitment").rglob("*.py")):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and node.value not in self.technical_values
                    and self.pattern.search(node.value)
                ):
                    offenders.append(f"{path.relative_to(self.root)}:{node.lineno}: {node.value[:80]}")
        self.assertEqual(offenders, [])
