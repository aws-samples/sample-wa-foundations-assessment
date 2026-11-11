import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import createWrapper from "@cloudscape-design/components/test-utils/dom";
import { afterEach, describe, expect, it } from "vitest";
import App, { REPOSITORY } from "./App.jsx";

function check(name, status, extra = {}) {
  return {
    check: name,
    description: `${name} description`,
    status,
    required: true,
    weight: 6,
    loe: 1,
    remediationLink: "https://docs.aws.amazon.com/example",
    ...extra,
  };
}

// The organization map's first column is the only markup indented by depth, so
// selecting on that inline margin scopes queries to the structure cells and
// avoids matching the same name in the Type column.
function orgStructureRows(section) {
  return [...section.querySelectorAll("div[style]")]
    .filter((element) => /^\d+px$/.test(element.style.marginLeft))
    .map((element) => ({
      // The name is the first inline element in the cell; the OU path and any
      // warning follow it, so read the name rather than the whole cell.
      label: element.querySelector("strong, span").textContent.trim(),
      indent: element.style.marginLeft,
    }));
}

// Mirrors the organization_map_summary payload built by _org_map_summary() in
// src/report/html_report.py: map-level fields only. The full tree stays in
// wafa-raw.json, so the HTML never embeds it twice.
function orgMapSummary(overrides = {}) {
  return {
    organizationId: "o-example12345",
    totals: { organizationalUnits: 1, accounts: 2, maxDepth: 1 },
    policyLookupErrors: [],
    depthTruncatedPaths: [],
    ...overrides,
  };
}

// Mirrors what _org_map_rows() in src/report/html_report.py puts in the
// payload: flattened rows with a stable key and finished policy labels. The UI
// renders these directly and never re-derives them.
function orgMapRows(overrides = []) {
  return overrides.length
    ? overrides
    : [
        {
          key: "root:r-root",
          kind: "root",
          name: "Root",
          id: "r-root",
          depth: 0,
          path: "Root",
          status: "",
          note: "",
          policies: [
            {
              id: "p-full",
              name: "FullAWSAccess",
              type: "SERVICE_CONTROL_POLICY",
              awsManaged: true,
              typeLabel: "SCP",
            },
          ],
        },
        {
          key: "account:111111111111",
          kind: "account",
          name: "Management",
          id: "111111111111",
          depth: 1,
          path: "Root",
          status: "ACTIVE",
          note: "",
          policies: [],
        },
        {
          key: "ou:ou-sec",
          kind: "ou",
          name: "Security",
          id: "ou-sec",
          depth: 1,
          path: "Root/Security",
          status: "",
          note: "",
          policies: [
            {
              id: "p-deny",
              name: "DenyRootUser",
              type: "SERVICE_CONTROL_POLICY",
              awsManaged: false,
              typeLabel: "SCP",
            },
          ],
        },
        {
          key: "account:333333333333",
          kind: "account",
          name: "Audit",
          id: "333333333333",
          depth: 2,
          path: "Root/Security",
          status: "SUSPENDED",
          note: "",
          policies: [
            {
              id: "p-acct",
              name: "AccountGuardrail",
              type: "SERVICE_CONTROL_POLICY",
              awsManaged: false,
              typeLabel: "SCP",
            },
          ],
        },
      ];
}

function reportData(overrides = {}) {
  return {
    checks: [
      check("AWS Organization exists", "complete"),
      check("Service Control Policies enabled", "incomplete"),
      check("Control Tower deployed", "error", { error: "AccessDenied" }),
    ],
    maturity: {
      level: 2,
      name: "Established",
      description: "AWS Organizations in place with OU structure.",
      next_level: 3,
      next_level_progress: 45.5,
      next_level_checks_needed: ["Service Control Policies enabled"],
      scoring_model: {
        rule: "The model assigns the highest maturity level whose required criteria are all complete.",
        weight_note: "Check weights prioritize recommended next steps.",
        levels: [
          {
            level: 1,
            name: "Base",
            description: "Baseline.",
            state: "achieved",
            criteria: [],
            complete_count: 0,
            criteria_count: 0,
          },
          {
            level: 2,
            name: "Established",
            description: "Established.",
            state: "current",
            criteria: [{ name: "AWS Organization exists", status: "complete" }],
            complete_count: 1,
            criteria_count: 1,
          },
          {
            level: 3,
            name: "Intermediate",
            description: "Intermediate.",
            state: "next",
            criteria: [
              { name: "Service Control Policies enabled", status: "incomplete" },
              { name: "Control Tower deployed", status: "error" },
              { name: "Cost and Usage Report configured", status: "not_assessed" },
            ],
            complete_count: 0,
            criteria_count: 3,
          },
          {
            level: 4,
            name: "Advanced",
            description: "Advanced.",
            state: "locked",
            criteria: [],
            complete_count: 0,
            criteria_count: 0,
          },
        ],
      },
    },
    delegated_administrators: [
      { accountId: "222222222222", accountName: "Security", services: ["guardduty.amazonaws.com"] },
    ],
    delegated_administrators_error: null,
    organization_map_summary: orgMapSummary(),
    organization_map_rows: orgMapRows(),
    organization_map_not_enabled_labels: ["RCP"],
    organization_map_error: null,
    account_info: {
      account_id: "111111111111",
      account_type: "management",
      is_management_account: true,
    },
    axis_scores: [
      { name: "Multi-Account Environment", score: 50, check_count: 2, assessed_count: 2 },
      { name: "Networking & Connectivity", score: 0, check_count: 0, assessed_count: 0 },
    ],
    summary: { total: 3, complete: 1, incomplete: 1, errors: 1, pct: 33 },
    ...overrides,
  };
}

function renderReport(overrides) {
  const result = render(<App data={reportData(overrides)} />);
  return { ...result, wrapper: createWrapper(result.container) };
}

afterEach(() => {
  cleanup();
  document.body.className = "";
});

describe("report UI", () => {
  it("renders every report section with Cloudscape navigation", () => {
    const { wrapper } = renderReport();
    expect(screen.getByRole("heading", { level: 1 }).textContent).toContain(
      "Well-Architected Foundations Assessment Report",
    );
    const nav = wrapper.findSideNavigation();
    const hrefs = [
      "#overview",
      "#maturity",
      "#capabilities",
      "#checks",
      "#org-map",
      "#delegated-admins",
      "#methodology",
    ];
    hrefs.forEach((href) => expect(nav.findLinkByHref(href)).not.toBeNull());
    expect(wrapper.findAppLayout()).not.toBeNull();
    expect(wrapper.findBarChart()).not.toBeNull();
    expect(wrapper.findSteps()).not.toBeNull();
  });

  it("shows maturity level, progress, and next steps", () => {
    const { wrapper } = renderReport();
    expect(screen.getByText("L2")).toBeTruthy();
    expect(wrapper.findProgressBar().getElement().textContent).toContain(
      "Progress toward Level 3",
    );
    const steps = wrapper.findSteps().getElement().textContent;
    expect(steps).toContain("Current level");
    expect(steps).toContain("Next target");
    expect(steps).toContain("Later level");
    expect(steps).toContain("1/1 criteria");
    expect(steps).toContain("Baseline");
  });

  it("distinguishes every criterion status in the methodology", () => {
    const { container } = renderReport();
    const methodology = container.querySelector("#methodology");
    const text = methodology.textContent;
    expect(text).toContain("Level 3 — Intermediate");
    expect(text).toContain("Not complete");
    expect(text).toContain("Error");
    expect(text).toContain("Not assessed");
    expect(text).toContain("highest maturity level whose required criteria are all complete");
  });

  it("filters check results by status", () => {
    const { wrapper } = renderReport();
    const table = wrapper.findTable();
    expect(table.findRows()).toHaveLength(3);
    wrapper.findSegmentedControl().findSegmentById("error").click();
    const rows = wrapper.findTable().findRows();
    expect(rows).toHaveLength(1);
    expect(rows[0].getElement().textContent).toContain("Control Tower deployed");
    expect(rows[0].getElement().textContent).toContain("AccessDenied");
  });

  it("filters check results by text", () => {
    const { wrapper } = renderReport();
    wrapper.findTextFilter().findInput().setInputValue("organization");
    expect(wrapper.findTable().findRows()).toHaveLength(1);
  });

  it("renders untrusted text as text", () => {
    const { container } = renderReport({
      checks: [check("<script>alert(1)</script>", "incomplete")],
    });
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<script>alert(1)</script>");
  });

  it("does not link non-HTTP remediation URLs", () => {
    const { container } = renderReport({
      checks: [check("Bad link", "incomplete", { remediationLink: "javascript:alert(1)" })],
    });
    expect(container.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("caveats limited account maturity", () => {
    const { wrapper } = renderReport({
      account_info: { account_id: "111111111111", account_type: "member", is_management_account: false },
    });
    const alert = wrapper.findAlert().getElement().textContent;
    expect(alert).toContain("Limited assessment");
    expect(alert).toContain("maturity level is provisional for the member account");
  });

  it("omits the delegated administrators section when empty", () => {
    const { container, wrapper } = renderReport({ delegated_administrators: [] });
    expect(container.querySelector("#delegated-admins")).toBeNull();
    expect(wrapper.findSideNavigation().findLinkByHref("#delegated-admins")).toBeNull();
  });

  it("reports the uncovered networking axis", () => {
    const { container } = renderReport();
    expect(container.querySelector("#capabilities").textContent).toContain(
      "Networking & Connectivity: no Phase 1 checks",
    );
  });

  it("marks axes with no assessed checks as not assessed", () => {
    const { container } = renderReport({
      axis_scores: [
        { name: "Cloud Financial Management", score: 0, check_count: 3, assessed_count: 0 },
      ],
    });
    expect(container.querySelector("#capabilities").textContent).toContain(
      "Cloud Financial Management: not assessed in this account mode",
    );
  });

  it("explains an undetermined account type", () => {
    const { container } = renderReport({
      account_info: {
        account_id: "111111111111",
        account_type: "unknown",
        is_management_account: false,
        detection_error: "AccessDeniedException",
      },
    });
    const overview = container.querySelector("#overview").textContent;
    expect(overview).toContain("Account type could not be determined");
    expect(overview).toContain("AccessDeniedException");
  });

  it("shows delegated administrator errors", () => {
    const { container } = renderReport({
      delegated_administrators: [
        {
          accountId: "222222222222",
          accountName: "Audit",
          services: [],
          servicesError: "ThrottlingException",
        },
      ],
      delegated_administrators_error: "AccessDeniedException",
    });
    const section = container.querySelector("#delegated-admins").textContent;
    expect(section).toContain("could not be fully listed");
    expect(section).toContain("AccessDeniedException");
    expect(section).toContain("Could not list services: ThrottlingException");
  });

  it("shows the delegated administrators section when listing failed", () => {
    const { container } = renderReport({
      delegated_administrators: [],
      delegated_administrators_error: "AccessDeniedException",
    });
    expect(container.querySelector("#delegated-admins")).not.toBeNull();
  });

  it("toggles between light and dark mode from the top navigation", () => {
    const { container } = renderReport();
    // TopNavigation also renders hidden copies of utilities to measure
    // overflow, so select the toggle by its accessible label.
    const toggle = (label) =>
      container.querySelector(`#wafa-top-nav [aria-label="${label}"]`);
    fireEvent.click(toggle("Switch to dark mode"));
    expect(document.body.classList.contains("awsui-dark-mode")).toBe(true);
    fireEvent.click(toggle("Switch to light mode"));
    expect(document.body.classList.contains("awsui-dark-mode")).toBe(false);
  });

  it("lists delegated administrators", () => {
    const { container } = renderReport();
    const section = within(container.querySelector("#delegated-admins"));
    expect(section.getByText("222222222222")).toBeTruthy();
    expect(section.getByText("guardduty.amazonaws.com")).toBeTruthy();
  });

  it("maps the OU hierarchy, account placement, and attached policies", () => {
    const { container } = renderReport();
    const section = container.querySelector("#org-map");
    const text = section.textContent;
    expect(text).toContain("o-example12345");
    // Root, its account, the child OU, and that OU's account.
    expect(orgStructureRows(section).map((row) => row.label)).toEqual([
      "Root",
      "Management",
      "Security",
      "Audit",
    ]);
    // Directly attached policies are labelled by type.
    expect(text).toContain("SCP: FullAWSAccess");
    expect(text).toContain("SCP: DenyRootUser");
    // A suspended account is surfaced rather than silently dropped.
    expect(text).toContain("SUSPENDED");
  });

  it("indents the organization map by hierarchy depth", () => {
    const { container } = renderReport();
    const rows = orgStructureRows(container.querySelector("#org-map"));
    // Accounts sit one level below the OU that contains them, so Management
    // (in Root) and the Security OU share an indent, and Audit sits deeper.
    expect(rows).toEqual([
      { label: "Root", indent: "0px" },
      { label: "Management", indent: "18px" },
      { label: "Security", indent: "18px" },
      { label: "Audit", indent: "36px" },
    ]);
  });

  it("pages the organization map instead of rendering every row", () => {
    // A large organization would otherwise render thousands of rows, each with
    // policy badges, on first load.
    const rows = Array.from({ length: 120 }, (_, index) => ({
      key: `account:${index}`,
      kind: "account",
      name: `Account ${index}`,
      id: `${index}`,
      depth: 1,
      path: "Root",
      status: "ACTIVE",
      note: "",
      policies: [],
    }));
    const { container } = renderReport({ organization_map_rows: rows });
    const section = container.querySelector("#org-map");
    expect(orgStructureRows(section)).toHaveLength(50);
    expect(section.textContent).not.toContain("Account 119");
  });

  it("filters the organization map and keeps each row locatable", () => {
    const { container } = renderReport();
    const section = container.querySelector("#org-map");
    fireEvent.change(section.querySelector("input"), {
      target: { value: "Audit" },
    });
    expect(orgStructureRows(section).map((row) => row.label)).toEqual(["Audit"]);
    // Paging and filtering both separate a row from its ancestors, so every
    // row shows the OU path that places it.
    expect(section.textContent).toContain("Root/Security");
  });

  it("notes guardrail policy types that are not enabled", () => {
    const { container } = renderReport();
    expect(container.querySelector("#org-map").textContent).toContain(
      "RCP not enabled in this organization",
    );
  });

  it("shows account-level policy attachments", () => {
    const { container } = renderReport();
    expect(container.querySelector("#org-map").textContent).toContain(
      "SCP: AccountGuardrail",
    );
  });

  it("warns when policy attachments could not be fully listed", () => {
    const { container } = renderReport({
      organization_map_summary: orgMapSummary({
        policyLookupErrors: [
          { scope: "INSPECTOR_POLICY", error: "TooManyRequestsException" },
        ],
      }),
    });
    const section = container.querySelector("#org-map").textContent;
    expect(section).toContain("Attached policies may be incomplete");
    expect(section).toContain("INSPECTOR_POLICY");
    expect(section).toContain("TooManyRequestsException");
  });

  it("names the OUs where the hierarchy walk stopped", () => {
    // The map-level warning locates the gap; the affected row carries the
    // explanation, so neither surface restates the other.
    const { container } = renderReport({
      organization_map_summary: orgMapSummary({
        depthTruncatedPaths: ["Root/Security/A/B/C/Deepest"],
      }),
    });
    const section = container.querySelector("#org-map").textContent;
    expect(section).toContain("nested deeper than the levels this map walks");
    expect(section).toContain("Root/Security/A/B/C/Deepest");
  });

  it("surfaces per-node organization map warnings", () => {
    const rows = orgMapRows();
    rows[2].note = "AccessDenied listing accounts";
    const { container } = renderReport({ organization_map_rows: rows });
    expect(container.querySelector("#org-map").textContent).toContain(
      "AccessDenied listing accounts",
    );
  });

  it("shows the organization map section when the map could not be built", () => {
    const { container } = renderReport({
      organization_map_summary: null,
      organization_map_error: "AccessDeniedException",
    });
    const section = container.querySelector("#org-map");
    expect(section).not.toBeNull();
    expect(section.textContent).toContain("could not be built");
    expect(section.textContent).toContain("AccessDeniedException");
  });

  it("omits the organization map section in limited mode", () => {
    const { container, wrapper } = renderReport({
      organization_map_summary: null,
      organization_map_rows: [],
      organization_map_error: null,
    });
    expect(container.querySelector("#org-map")).toBeNull();
    expect(wrapper.findSideNavigation().findLinkByHref("#org-map")).toBeNull();
  });

  it("renders untrusted OU names as text", () => {
    const rows = orgMapRows();
    rows[2].name = "<script>alert(1)</script>";
    const { container } = renderReport({ organization_map_rows: rows });
    const section = container.querySelector("#org-map");
    expect(section.querySelector("script")).toBeNull();
    expect(section.textContent).toContain("<script>alert(1)</script>");
  });

  it("renders the policy label supplied by the report payload", () => {
    const rows = orgMapRows();
    // An unrecognized type is labelled in Python; the UI must not re-derive it.
    rows[0].policies = [
      {
        id: "p-new",
        name: "NewThing",
        type: "SOME_NEW_POLICY",
        awsManaged: false,
        typeLabel: "Some New Policy",
      },
    ];
    const { container } = renderReport({ organization_map_rows: rows });
    expect(container.querySelector("#org-map").textContent).toContain(
      "Some New Policy: NewThing",
    );
  });

  it("links to the GitHub repository in the footer", () => {
    const { container } = renderReport();
    const footer = container.querySelector("footer");
    expect(footer.textContent).toContain("aws-samples/sample-wa-foundations-assessment");
    const link = footer.querySelector(`a[href="${REPOSITORY.url}"]`);
    expect(link).not.toBeNull();
    expect(REPOSITORY.url).toBe(
      "https://github.com/aws-samples/sample-wa-foundations-assessment",
    );
  });
});
