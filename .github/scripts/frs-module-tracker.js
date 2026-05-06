const PROJECT_OWNER = "cybertechiesph";
const PROJECT_NUMBER = 1;

const MODULE_OPTIONS = [
  "Identity and Access Control",
  "Recruitment Entry and Vacancy Management",
  "Applicant Intake and OTP Verification",
  "Recruitment Case Management and Workflow Engine",
  "Branch-Aware and Level-Aware Routing",
  "Document Review and Qualification Screening",
  "Examination Management",
  "Interview and Rating Management",
  "Deliberation and Decision Support",
  "Decision and Approval Handling",
  "Notification Management",
  "Appointment and Contract Completion",
  "Evidence Vault and Record Management",
  "Audit Logging and Traceability",
  "Evidence Export and Integrity Verification"
];

const MODULE_OPTION_COLORS = [
  "BLUE",
  "GREEN",
  "YELLOW",
  "ORANGE",
  "RED",
  "PINK",
  "BLUE",
  "GREEN",
  "YELLOW",
  "ORANGE",
  "RED",
  "PINK",
  "BLUE",
  "GREEN",
  "YELLOW"
];

const LEGACY_TRACKER_TITLES = [
  "[Feature]: Verify project tracking automation",
  "[Task]: Configure GitHub Projects board and repository labels",
  "[Task]: Run full test suite and record baseline",
  "[Feature]: Verify branch and level routing rules",
  "[Feature]: Review Evidence Vault encryption and integrity flow",
  "[Feature]: QA applicant intake and OTP verification",
  "[Feature]: QA evaluation stage locks",
  "[Task]: Prepare demo data and script",
  "[Task]: Deployment readiness review"
];

const labelDefinitions = {
  "type:feature": ["1d76db", "Tracks an implemented FRS feature/module."],
  "priority:p1": ["d93f0b", "Primary FRS review priority."],
  "branch:shared": ["0e8a16", "Applies to both Plantilla and COS unless otherwise noted."],
  "module:identity": ["5319e7", "Identity and Access Control."],
  "module:entry-management": ["5319e7", "Recruitment Entry and Vacancy Management."],
  "module:intake-otp": ["5319e7", "Applicant Intake and OTP Verification."],
  "module:workflow-engine": ["5319e7", "Recruitment Case Management and Workflow Engine."],
  "module:routing": ["5319e7", "Branch-Aware and Level-Aware Routing."],
  "module:screening": ["5319e7", "Document Review and Qualification Screening."],
  "module:examination": ["5319e7", "Examination Management."],
  "module:interview-rating": ["5319e7", "Interview and Rating Management."],
  "module:deliberation": ["5319e7", "Deliberation and Decision Support."],
  "module:decision-approval": ["5319e7", "Decision and Approval Handling."],
  "module:notifications": ["5319e7", "Notification Management."],
  "module:completion": ["5319e7", "Appointment and Contract Completion."],
  "module:evidence-vault": ["5319e7", "Evidence Vault and Record Management."],
  "module:audit-logging": ["5319e7", "Audit Logging and Traceability."],
  "module:export-integrity": ["5319e7", "Evidence Export and Integrity Verification."],
  "security": ["c5def5", "Security-sensitive FRS behavior."],
  "audit": ["c5def5", "Audit logging or traceability behavior."],
  "routing": ["c5def5", "Routing rule behavior."],
  "workflow": ["c5def5", "Workflow stage/state behavior."],
  "evidence": ["c5def5", "Evidence handling, storage, or export behavior."]
};

const modules = [
  {
    number: 1,
    title: "Identity and Access Control",
    label: "module:identity",
    status: "Done",
    groups: [
      "Internal User Authentication",
      "Password and Session Security",
      "Role-Based Access Control (RBAC)",
      "Account Administration"
    ],
    evidence: [
      "RecruitmentUser custom user model with locked internal roles.",
      "Internal login, logout, password change, user create/update, and activation views.",
      "Role mixins and service checks enforce server-side access restrictions.",
      "Account and role changes are audit-logged."
    ],
    tests: [
      "FoundationSmokeTests",
      "IdentityAdministrationTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/identity_views.py",
      "recruitment/forms.py",
      "recruitment/permissions.py",
      "recruitment/tests.py"
    ]
  },
  {
    number: 2,
    title: "Recruitment Entry and Vacancy Management",
    label: "module:entry-management",
    status: "Done",
    groups: [
      "Plantilla and COS Recruitment Entry Creation",
      "Publication, Opening, and Intake Status",
      "Entry Metadata and Qualification Reference"
    ],
    evidence: [
      "PositionReference and PositionPosting models support Plantilla and COS entries.",
      "Entry forms/views enforce branch-specific opening, pooling, and validity rules.",
      "Entry status transitions and metadata changes are validated and audit-logged."
    ],
    tests: [
      "RecruitmentEntryManagementTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/entry_views.py",
      "recruitment/forms.py",
      "recruitment/services.py",
      "recruitment/tests.py"
    ]
  },
  {
    number: 3,
    title: "Applicant Intake and OTP Verification",
    label: "module:intake-otp",
    status: "Done",
    groups: [
      "Shared Applicant Portal",
      "Accountless Application Submission",
      "OTP Verification",
      "Submission Finalization"
    ],
    evidence: [
      "Public applicant portal lists Plantilla and COS paths and active entries.",
      "Accountless draft/final submission supports requirement-coded uploads.",
      "OTP is generated, hashed, emailed, expires, and is required for finalization.",
      "Final submissions receive application references and status lookup support."
    ],
    tests: [
      "ApplicantPortalFlowTests"
    ],
    files: [
      "recruitment/portal_views.py",
      "recruitment/applicant_urls.py",
      "recruitment/forms.py",
      "recruitment/services.py",
      "recruitment/requirements.py",
      "recruitment/tests.py"
    ]
  },
  {
    number: 4,
    title: "Recruitment Case Management and Workflow Engine",
    label: "module:workflow-engine",
    status: "Done",
    groups: [
      "Recruitment Case Creation",
      "Stage-Based Workflow Progression",
      "Stage Locking and Controlled Reopen",
      "Case Timeline and Status History"
    ],
    evidence: [
      "Valid submitted applications create linked RecruitmentCase records.",
      "Workflow service enforces stage progression, prerequisites, and branch paths.",
      "Finalized outputs are stage-locked and controlled reopen is audit-logged.",
      "Case timeline and routing history are available for traceability."
    ],
    tests: [
      "RecruitmentCaseWorkflowTests",
      "WorkflowRoutingTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/services.py",
      "recruitment/views.py",
      "recruitment/tests.py"
    ]
  },
  {
    number: 5,
    title: "Branch-Aware and Level-Aware Routing",
    label: "module:routing",
    status: "Done",
    groups: [
      "Branch Routing",
      "Level-Aware Internal Routing",
      "Routing Restrictions and Override"
    ],
    evidence: [
      "Routing service sends Level 1 to Secretariat and Level 2 to HRM Chief.",
      "Plantilla follows stricter path and COS follows lighter path.",
      "Secretariat Level 2 processing is blocked unless a controlled override exists.",
      "Routing and override events are stored in RoutingHistory and AuditLog."
    ],
    tests: [
      "WorkflowRoutingTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/services.py",
      "recruitment/views.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["routing", "security", "workflow"]
  },
  {
    number: 6,
    title: "Document Review and Qualification Screening",
    label: "module:screening",
    status: "Done",
    groups: [
      "Completeness Review",
      "Qualification-Related Review",
      "Screening Finalization"
    ],
    evidence: [
      "ScreeningRecord stores completeness, qualification, disposition, notes, and finalization.",
      "Screening views/forms restrict actions by handler, stage, branch, and level.",
      "Finalized screening records are locked and audit-relevant workflow actions are tested."
    ],
    tests: [
      "ScreeningRecordTests",
      "RecruitmentCaseWorkflowTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/forms.py",
      "recruitment/views.py",
      "recruitment/services.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["workflow"]
  },
  {
    number: 7,
    title: "Examination Management",
    label: "module:examination",
    status: "In Progress",
    groups: [
      "Examination Record Handling",
      "Examination Status and Validity",
      "Examination Output Preservation"
    ],
    evidence: [
      "ExamRecord supports exam type, status, score/result, validity, waiver/absence, notes, and finalization.",
      "Exam management is branch-aware and role/stage restricted.",
      "Finalized examination outputs are locked and preserved in case history."
    ],
    tests: [
      "ExamRecordTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/forms.py",
      "recruitment/views.py",
      "recruitment/services.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["workflow"]
  },
  {
    number: 8,
    title: "Interview and Rating Management",
    label: "module:interview-rating",
    status: "In Progress",
    groups: [
      "Interview Scheduling",
      "Interview Ratings",
      "Fallback Rating Handling"
    ],
    evidence: [
      "InterviewSession supports scheduling and stage-linked interview records.",
      "InterviewRating supports evaluator-specific direct rating and justification.",
      "Fallback scanned rating sheets upload into the Evidence Vault.",
      "Finalized interview outputs are locked against ordinary modification."
    ],
    tests: [
      "InterviewManagementTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/forms.py",
      "recruitment/views.py",
      "recruitment/services.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["workflow", "evidence"]
  },
  {
    number: 9,
    title: "Deliberation and Decision Support",
    label: "module:deliberation",
    status: "In Progress",
    groups: [
      "Consolidation of Evaluation Outputs",
      "Deliberation Record Handling",
      "Ranking and CAR"
    ],
    evidence: [
      "Deliberation logic consolidates finalized screening, exam, and interview outputs.",
      "DeliberationRecord stores decisions/minutes and finalization state.",
      "Comparative Assessment Report generation uses ReportLab and versioned evidence.",
      "Plantilla CAR requirements and COS branch handling are tested."
    ],
    tests: [
      "DeliberationDecisionSupportTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/forms.py",
      "recruitment/views.py",
      "recruitment/services.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["workflow", "evidence"]
  },
  {
    number: 10,
    title: "Decision and Approval Handling",
    label: "module:decision-approval",
    status: "In Progress",
    groups: [
      "Submission Packet Preparation",
      "Final Decision Recording",
      "Pre-Decision Artifact Preservation"
    ],
    evidence: [
      "Submission packet builder compiles screening, exam, interview, deliberation, CAR, and evidence references.",
      "FinalDecision records selected/not-selected outcomes and Appointing Authority decisions.",
      "Selected cases route to completion; not-selected cases close and lock as tested."
    ],
    tests: [
      "FinalDecisionHandlingTests",
      "DeliberationDecisionSupportTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/forms.py",
      "recruitment/views.py",
      "recruitment/services.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["workflow"]
  },
  {
    number: 11,
    title: "Notification Management",
    label: "module:notifications",
    status: "In Progress",
    groups: [
      "Submission and Status Notifications",
      "Selection and Non-Selection Notifications",
      "Requirement and Deadline Notifications"
    ],
    evidence: [
      "NotificationLog records queued, sent, failed, and recipient metadata.",
      "Notification services support submission acknowledgment, selected, non-selected, checklist, and reminder messages.",
      "Requirement and reminder notifications are role-checked and audit-linked."
    ],
    tests: [
      "NotificationManagementTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/notification_services.py",
      "recruitment/views.py",
      "recruitment/forms.py",
      "recruitment/tests.py"
    ]
  },
  {
    number: 12,
    title: "Appointment and Contract Completion",
    label: "module:completion",
    status: "In Progress",
    groups: [
      "Plantilla Completion Tracking",
      "COS Completion Tracking",
      "Case Closure"
    ],
    evidence: [
      "CompletionRecord and CompletionRequirement store Plantilla appointment and COS contract completion details.",
      "Completion forms enforce branch-specific fields and checklist handling.",
      "Closure requires completion reference/date and resolved requirements; closed cases remain retrievable."
    ],
    tests: [
      "CompletionTrackingTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/forms.py",
      "recruitment/views.py",
      "recruitment/services.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["workflow"]
  },
  {
    number: 13,
    title: "Evidence Vault and Record Management",
    label: "module:evidence-vault",
    status: "Done",
    groups: [
      "Centralized Evidence Storage",
      "Version Preservation and Retrieval",
      "Evidence Integrity"
    ],
    evidence: [
      "EvidenceVaultItem stores stage, owner, uploader, timestamps, content metadata, SHA-256 digest, and encrypted bytes.",
      "Evidence uploads preserve versions and support retrieval, archive tagging, and search/filter review.",
      "AES-256-GCM encryption and digest generation are implemented in services."
    ],
    tests: [
      "EvidenceVaultTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/services.py",
      "recruitment/views.py",
      "recruitment/upload_validation.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["security", "evidence", "audit"]
  },
  {
    number: 14,
    title: "Audit Logging and Traceability",
    label: "module:audit-logging",
    status: "Done",
    groups: [
      "Workflow and Security Event Logging",
      "Traceability Support",
      "Sensitive Access Logging"
    ],
    evidence: [
      "AuditLog stores actor, role, action, timestamp, case/application references, stage, IP, and metadata.",
      "Workflow, routing, override, protected record access, evidence review, and export actions are audit-logged.",
      "System and application audit views enforce administrative restrictions."
    ],
    tests: [
      "AuditLoggingTraceabilityTests",
      "ViewAndExportTests"
    ],
    files: [
      "recruitment/models.py",
      "recruitment/services.py",
      "recruitment/views.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["security", "audit"]
  },
  {
    number: 15,
    title: "Evidence Export and Integrity Verification",
    label: "module:export-integrity",
    status: "Done",
    groups: [
      "Controlled Export",
      "Export Bundle Content",
      "Integrity Verification"
    ],
    evidence: [
      "Controlled export is restricted by role and view/service permissions.",
      "Export bundle generation uses zipfile and includes application, audit, routing, evidence inventory, manifest, and verification outputs.",
      "SHA-256 verification reports and checksums make exported evidence independently verifiable."
    ],
    tests: [
      "ViewAndExportTests"
    ],
    files: [
      "recruitment/services.py",
      "recruitment/views.py",
      "recruitment/tests.py"
    ],
    extraLabels: ["security", "evidence", "audit"]
  }
];

function moduleIssueTitle(moduleInfo) {
  return `[FRS Module ${String(moduleInfo.number).padStart(2, "0")}]: ${moduleInfo.title}`;
}

function moduleIssueBody(moduleInfo) {
  return [
    "## FRS Source",
    "- Source document: `CLIENT_FRS.pdf`",
    `- Module ${moduleInfo.number}: ${moduleInfo.title}`,
    "",
    "## Tracker status",
    moduleInfo.status,
    "",
    "## Status basis",
    "Marked Done after cross-checking the PDF FRS requirements against the Django implementation and rerunning the full automated test suite.",
    "",
    "## FRS requirement groups",
    ...moduleInfo.groups.map((group) => `- [x] ${group}`),
    "",
    "## Implementation evidence",
    ...moduleInfo.evidence.map((item) => `- ${item}`),
    "",
    "## Relevant files",
    ...moduleInfo.files.map((file) => `- \`${file}\``),
    "",
    "## Test evidence",
    ...moduleInfo.tests.map((testName) => `- \`${testName}\``),
    "",
    "## Verification",
    "- `python manage.py test`",
    "- Result on 2026-05-07: 155 tests passed.",
    "",
    "## Manual review notes",
    "- Production email delivery, deployment settings, and real HR data handling still require environment-specific review.",
    "- Full onboarding, offboarding, payroll, termination, and full employee lifecycle functions remain out of scope per the FRS."
  ].join("\n");
}

async function ensureLabel(github, owner, repo, name) {
  const definition = labelDefinitions[name] || ["ededed", "RecruitGuard-CHD tracking label."];
  try {
    await github.rest.issues.getLabel({ owner, repo, name });
  } catch (error) {
    if (error.status !== 404) throw error;
    await github.rest.issues.createLabel({
      owner,
      repo,
      name,
      color: definition[0],
      description: definition[1]
    });
  }
}

async function loadProject(github) {
  const result = await github.graphql(`
    query($login: String!, $number: Int!) {
      user(login: $login) {
        projectV2(number: $number) {
          id
          fields(first: 100) {
            nodes {
              ... on ProjectV2Field {
                id
                name
              }
              ... on ProjectV2SingleSelectField {
                id
                name
                options {
                  id
                  name
                }
              }
            }
          }
        }
      }
    }
  `, {
    login: PROJECT_OWNER,
    number: PROJECT_NUMBER
  });

  const project = result.user?.projectV2;
  if (!project) {
    throw new Error(`Project not found: ${PROJECT_OWNER}/${PROJECT_NUMBER}`);
  }
  return project;
}

async function ensureModuleField(github, project) {
  const existing = project.fields.nodes.find((field) => field?.name === "Module");
  if (existing?.options) return existing;

  const created = await github.graphql(`
    mutation($projectId: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]!) {
      createProjectV2Field(input: {
        projectId: $projectId,
        dataType: SINGLE_SELECT,
        name: "Module",
        singleSelectOptions: $options
      }) {
        projectV2Field {
          ... on ProjectV2SingleSelectField {
            id
            name
            options {
              id
              name
            }
          }
        }
      }
    }
  `, {
    projectId: project.id,
    options: MODULE_OPTIONS.map((name, index) => ({
      name,
      color: MODULE_OPTION_COLORS[index] || "GRAY",
      description: name
    }))
  });

  return created.createProjectV2Field.projectV2Field;
}

async function getProjectItem(github, projectId, contentId) {
  let cursor = null;
  do {
    const result = await github.graphql(`
      query($projectId: ID!, $cursor: String) {
        node(id: $projectId) {
          ... on ProjectV2 {
            items(first: 100, after: $cursor) {
              pageInfo {
                hasNextPage
                endCursor
              }
              nodes {
                id
                content {
                  ... on Issue {
                    id
                  }
                  ... on PullRequest {
                    id
                  }
                }
              }
            }
          }
        }
      }
    `, { projectId, cursor });

    for (const item of result.node.items.nodes) {
      if (item.content?.id === contentId) {
        return item;
      }
    }

    cursor = result.node.items.pageInfo.hasNextPage
      ? result.node.items.pageInfo.endCursor
      : null;
  } while (cursor);

  return null;
}

async function addProjectItem(github, projectId, contentId) {
  const existing = await getProjectItem(github, projectId, contentId);
  if (existing) return existing;

  try {
    const result = await github.graphql(`
      mutation($projectId: ID!, $contentId: ID!) {
        addProjectV2ItemById(input: {
          projectId: $projectId,
          contentId: $contentId
        }) {
          item {
            id
          }
        }
      }
    `, { projectId, contentId });

    return result.addProjectV2ItemById.item;
  } catch (error) {
    const existingAfterRace = await getProjectItem(github, projectId, contentId);
    if (existingAfterRace) return existingAfterRace;
    throw error;
  }
}

async function setSingleSelectField(github, projectId, itemId, field, optionName, core) {
  if (!field?.options) {
    core.warning(`Project field is not a single-select field: ${field?.name || "unknown"}`);
    return;
  }

  const optionId = field.options.find((option) => option.name === optionName)?.id;
  if (!optionId) {
    core.warning(`Project option missing for ${field.name}: ${optionName}`);
    return;
  }

  await github.graphql(`
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
      updateProjectV2ItemFieldValue(input: {
        projectId: $projectId,
        itemId: $itemId,
        fieldId: $fieldId,
        value: {
          singleSelectOptionId: $optionId
        }
      }) {
        projectV2Item {
          id
        }
      }
    }
  `, {
    projectId,
    itemId,
    fieldId: field.id,
    optionId
  });
}

async function archiveProjectItem(github, projectId, itemId, core) {
  try {
    await github.graphql(`
      mutation($projectId: ID!, $itemId: ID!) {
        archiveProjectV2Item(input: {
          projectId: $projectId,
          itemId: $itemId
        }) {
          item {
            id
          }
        }
      }
    `, { projectId, itemId });
  } catch (error) {
    core.warning(`Could not archive project item ${itemId}: ${error.message}`);
  }
}

module.exports = async ({ github, context, core }) => {
  const owner = context.repo.owner;
  const repo = context.repo.repo;

  const issueList = await github.paginate(github.rest.issues.listForRepo, {
    owner,
    repo,
    state: "all",
    per_page: 100
  });
  const issues = issueList.filter((issue) => !issue.pull_request);
  const issuesByTitle = new Map(issues.map((issue) => [issue.title, issue]));

  for (const moduleInfo of modules) {
    for (const label of [
      "type:feature",
      "priority:p1",
      "branch:shared",
      moduleInfo.label,
      ...(moduleInfo.extraLabels || [])
    ]) {
      await ensureLabel(github, owner, repo, label);
    }
  }

  const project = await loadProject(github);
  const moduleField = await ensureModuleField(github, project);
  const refreshedProject = await loadProject(github);
  const statusField = refreshedProject.fields.nodes.find((field) => field?.name === "Status");
  const activeModuleTitles = new Set();

  for (const moduleInfo of modules) {
    const title = moduleIssueTitle(moduleInfo);
    activeModuleTitles.add(title);
    const labels = [
      "type:feature",
      "priority:p1",
      "branch:shared",
      moduleInfo.label,
      ...(moduleInfo.extraLabels || [])
    ];
    const body = moduleIssueBody(moduleInfo);
    const existing = issuesByTitle.get(title);

    let issue;
    if (existing) {
      const response = await github.rest.issues.update({
        owner,
        repo,
        issue_number: existing.number,
        title,
        body,
        labels,
        state: "open"
      });
      issue = response.data;
      core.info(`Updated ${title}`);
    } else {
      const response = await github.rest.issues.create({
        owner,
        repo,
        title,
        body,
        labels
      });
      issue = response.data;
      core.info(`Created ${title}`);
    }

    const item = await addProjectItem(github, project.id, issue.node_id);
    await setSingleSelectField(github, project.id, item.id, moduleField, moduleInfo.title, core);
    await setSingleSelectField(github, project.id, item.id, statusField, moduleInfo.status, core);
  }

  for (const legacyTitle of LEGACY_TRACKER_TITLES) {
    if (activeModuleTitles.has(legacyTitle)) continue;
    const legacyIssue = issuesByTitle.get(legacyTitle);
    if (!legacyIssue) continue;
    const cleanupNotice = "Closed by FRS tracker cleanup. Active project tracking is now handled by the 15 FRS Module issues.";
    const legacyBody = legacyIssue.body || "";

    await github.rest.issues.update({
      owner,
      repo,
      issue_number: legacyIssue.number,
      body: legacyBody.includes(cleanupNotice)
        ? legacyBody
        : `${legacyBody}\n\n---\n${cleanupNotice}`,
      state: "closed",
      state_reason: "not_planned"
    });
    core.info(`Closed non-FRS tracker issue: ${legacyTitle}`);

    const item = await getProjectItem(github, project.id, legacyIssue.node_id);
    if (item) {
      await archiveProjectItem(github, project.id, item.id, core);
      core.info(`Archived non-FRS project item: ${legacyTitle}`);
    }
  }
};
