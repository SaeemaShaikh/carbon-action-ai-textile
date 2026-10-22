# CARBON-ACTION AI — Project Plan

**Project Title:** CARBON-ACTION AI — AI-Driven Industrial Climate Intervention Intelligence  
**Project Domain:** Artificial Intelligence for Climate Change  
**Initial Industry Focus:** Textile Dyeing and Printing  
**Initial Use Case:** Boiler Operations and Steam-Related Energy Analysis  
**Project Status:** Prototype Under Development  
**Prototype Target Deadline:** 10 November 2026  
**Project Type:** AI-Based Industrial Decision Support System

---

## 1. Project Overview

CARBON-ACTION AI is an AI-driven industrial decision-support system designed to help facilities move beyond emissions monitoring toward evidence-based operational and climate-action decisions.

Industrial facilities may collect data about energy consumption, fuel use, steam generation, operating conditions and emissions. However, identifying which operational intervention is appropriate, estimating its potential impact and verifying the outcome requires connecting these sources of information.

CARBON-ACTION AI aims to connect industrial data analysis with facility-specific intervention selection, constrained scenario simulation, recommendation ranking and outcome verification.

The initial prototype will focus on textile dyeing and printing facilities, with particular attention to boiler operations and steam-related energy use.

The system will initially function as a decision-support tool. It will not directly control industrial equipment or make safety-critical operating adjustments.

## 2. Problem Statement

Textile processing facilities use energy-intensive equipment, including boilers, dyeing machines, stenters, dryers, pumps, fans and other utilities.

Boilers can supply steam or heat to processes such as dyeing, washing and other finishing operations. Their fuel consumption, operating conditions and combustion-related measurements can provide useful information for energy analysis.

However, identifying a suitable intervention requires more than observing an abnormal value. The system must understand the available measurements, operating conditions, production requirements, equipment constraints and evidence supporting a proposed action.

The problem addressed by CARBON-ACTION AI is:

**How can industrial energy and operational data be converted into evidence-supported intervention decisions whose potential impacts can be evaluated and whose actual outcomes can subsequently be verified?**

## 3. Project Objectives

The primary objectives are to:

1. Develop a reusable data pipeline for industrial energy and operational measurements.
2. Analyse boiler-related operating patterns and identify unusual behaviour.
3. Establish a baseline of expected behaviour using appropriate statistical or machine-learning methods.
4. Identify variables associated with detected deviations without automatically treating correlation as causation.
5. Develop an intervention library containing applicable actions, required measurements and operational constraints.
6. Implement an Evidence Gate to determine whether an intervention is adequately supported by the available data.
7. Compare a baseline with a defined intervention scenario where the required impact-estimation method is available.
8. Rank eligible interventions using configurable climate, energy, cost, production, feasibility and operational-risk criteria.
9. Present findings through an interactive dashboard.
10. Record decisions and support future verification using actual post-intervention measurements.

## 4. Initial Scope

### 4.1 Included in the prototype

- Industrial dataset acquisition and documentation.
- Data ingestion, validation and standardisation.
- Exploratory data analysis.
- Boiler operating-pattern analysis.
- Baseline modelling and opportunity detection.
- Likely-driver analysis.
- Facility-specific action eligibility.
- Evidence Gate and recommendation blocking.
- One end-to-end intervention or investigation scenario.
- Scenario comparison where technically supported.
- Recommendation ranking and explanations.
- Streamlit dashboard.
- Action history and verification status.
- Testing, documentation and prototype demonstration.

### 4.2 Excluded from the initial prototype

- Autonomous factory or boiler control.
- Automatic changes to FD fan, ID fan, pressure or combustion setpoints.
- A complete digital twin of the textile facility.
- Deep reinforcement learning without suitable training and intervention data.
- Carbon-credit issuance or certification.
- Automatic compliance certification.
- Quantified carbon-saving claims unsupported by the required measurements or calculation methods.
- Full optimisation of every textile production machine.
- Claims that the prototype has been validated at a commercial textile plant unless appropriate plant-specific evidence becomes available.

## 5. Textile Industry Understanding

The system should represent the production equipment and shared utilities separately.

### Textile production equipment

Depending on the facility, the production process may include:

- Scouring and bleaching equipment.
- Dyeing machines.
- Washing and rinsing ranges.
- Printing machines.
- Drying and stenter machines.
- Finishing equipment.

### Utilities and supporting systems

- Boilers and steam distribution.
- Fuel supply and feedwater systems.
- Forced Draft (FD) and Induced Draft (ID) fans.
- Pumps and circulation systems.
- Compressors.
- Electrical distribution.
- Condensate-return systems.
- Water and effluent treatment.

Not every textile facility contains every equipment type. The actual facility configuration must be established before an intervention is recommended.

Boiler analysis will be the initial focus because boiler operation can connect fuel consumption, steam demand, combustion conditions and utility performance.

Changes to boiler or fan settings must remain subject to approved operating limits and qualified human review.

## 6. End-to-End Technical Pipeline

The proposed pipeline is:

1. **Industrial Process Understanding:** Map the production process, utility systems, emissions sources and operating constraints.
2. **Data Acquisition:** Identify available public datasets and authorised facility data.
3. **Data Validation:** Inspect timestamps, measurement units, missing readings, duplicates, sensor ranges and sampling frequency.
4. **Canonical Data Mapping:** Standardise source fields into a common industrial data schema.
5. **Exploratory Data Analysis:** Examine trends, distributions, operating patterns and variable relationships.
6. **Baseline Modelling:** Estimate expected behaviour under comparable operating conditions.
7. **Opportunity Detection:** Identify deviations, peaks or unusual energy and operating patterns.
8. **Likely-Driver Analysis:** Examine variables associated with each detected deviation.
9. **Action Generation:** Identify candidate interventions from the action library.
10. **Evidence Gate:** Check whether each intervention has the required inputs, controllable variables, constraints and an appropriate impact-estimation method.
11. **Scenario Simulation:** Compare baseline and intervention scenarios when the required assumptions and calculation methods are available.
12. **Action Ranking:** Rank eligible interventions according to the selected decision priorities.
13. **Recommendation:** Present the proposed action, rationale, evidence, assumptions, estimated impact and risks.
14. **Verification:** Compare actual post-intervention outcomes with an appropriate baseline when measurements become available.
15. **Learning:** Store intervention history and use suitable verified outcomes to improve future analysis.

## 7. Data Strategy

### 7.1 Dataset selection

Datasets will be evaluated according to:

- Industrial provenance.
- Relevance to boiler or textile operations.
- Time-series coverage.
- Energy and fuel measurements.
- Emissions measurements and units.
- Production information.
- Operational and control variables.
- Data quality.
- Licensing and permitted use.
- Suitability for modelling and scenario analysis.

A reference industrial boiler dataset may be used to develop and test process-analysis components if actual textile-plant data is unavailable.

Such a dataset must retain its original source and facility context. It must not be described as data collected from the textile factory.

### 7.2 Data provenance

Every important value must be identified as one of the following:

- **MEASURED:** Directly recorded in the source data.
- **DERIVED:** Calculated from documented source measurements using a defined formula.
- **MODELLED / SIMULATED:** Estimated by a model or generated by a scenario calculation.

Simulated results must never be represented as measured industrial savings.

### 7.3 Expected data categories

The canonical data model may include:

- Facility and asset identifiers.
- Timestamps and sampling intervals.
- Electricity consumption and demand.
- Fuel quantity and fuel type.
- Steam flow, pressure and temperature.
- Boiler operating measurements.
- FD and ID fan measurements.
- Emissions-monitor readings and verified units.
- Production quantity, batch identifier and operating time.
- Available control variables.
- Safety, quality and production constraints.
- Source information and data-quality indicators.

The availability of each field must be confirmed from the actual source data. Missing information must not be fabricated.

## 8. AI and Decision Engine

### 8.1 Baseline modelling

Establish expected behaviour using a simple historical or statistical baseline. Compare it with an appropriate machine-learning model only when the data supports the comparison.

### 8.2 Opportunity detection

Identify unusual measurements or deviations from expected behaviour.

A detected deviation is a candidate for investigation, not automatic proof of inefficiency, equipment failure or emissions reduction potential.

### 8.3 Likely-driver analysis

Use available process variables, temporal analysis and suitable model-explanation methods to identify variables associated with the deviation.

Reports should distinguish a likely driver from a proven cause.

### 8.4 Evidence Gate

Every candidate action must be checked for:

1. Availability of required measurements.
2. Applicability to the configured facility and asset.
3. Availability of a suitable controllable variable, where control is relevant.
4. Availability of necessary production and safety constraints.
5. Availability of a defensible impact-estimation method.
6. Compliance with hard operating constraints.

If a mandatory condition is not met, the action must be blocked or restricted to investigation.

### 8.5 Evidence levels

- **E0 — No evidence:** Insufficient support; do not recommend.
- **E1 — Engineering knowledge:** Technically plausible, but a facility-specific impact estimate is not adequately supported.
- **E2 — Facility association:** Historical facility data supports an observed association.
- **E3 — Validated model:** A relevant model has been evaluated on held-out data.
- **E4 — Observed intervention:** Actual post-intervention outcomes have been recorded.

Evidence levels describe the support for a recommendation. They must not be confused with a model-confidence percentage.

### 8.6 Scenario simulation

Compare a defined baseline with a specified alternative scenario.

Every scenario must document its inputs, assumptions, constraints, equations and provenance. If the available information cannot support a valid impact estimate, the system must withhold the estimate.

### 8.7 Recommendation ranking

Eligible actions may be ranked according to configurable priorities such as:

- Expected carbon benefit.
- Energy benefit.
- Cost impact.
- Production implications.
- Implementation feasibility.
- Operational and safety risk.
- Strength of evidence.

Hard safety and production constraints must be enforced before ranking. An action that violates a hard constraint must not remain eligible because of a high score.

## 9. Initial Intervention Library

The initial library will be deliberately limited.

Potential candidates include:

1. Investigate unusual boiler fuel-consumption patterns.
2. Investigate abnormal boiler operating conditions.
3. Examine unusual FD/ID fan operating patterns when the relevant measurements are available.
4. Investigate possible energy-consumption anomalies in relevant textile equipment.
5. Investigate idle-energy opportunities when operating-state evidence is available.
6. Evaluate a constrained energy-demand scenario when the necessary flexibility and operating-window information exists.
7. Investigate condensate recovery opportunities when flow, temperature and system-configuration information are available.

Each intervention will specify its applicable system, required inputs, constraints, evidence requirements, risk level and impact-estimation method.

These actions are candidates for investigation or evaluation. They are not automatic instructions to modify equipment settings.

## 10. Dashboard Requirements

The Streamlit dashboard will eventually include the following pages.

### 10.1 Plant Overview

- Available energy and fuel measurements.
- Steam and production indicators when available.
- Verified emissions-monitor indicators.
- Time-series trends.
- Data-quality and provenance status.

### 10.2 Boiler Intelligence

- Boiler operating trends.
- Available FD/ID fan measurements.
- Relevant temperatures, pressures and oxygen readings.
- Detected operating-pattern deviations.
- Measurement quality and missing-data warnings.

### 10.3 AI Diagnosis

- Description of detected deviations.
- Observed and expected values.
- Likely associated drivers.
- Evidence supporting the finding.
- Important uncertainties and missing information.

### 10.4 Action Simulator

- Baseline and scenario comparison.
- Input assumptions and operating constraints.
- Calculated or modelled impact.
- Energy, cost and carbon outputs where justified.
- Clear MEASURED, DERIVED or MODELLED / SIMULATED labels.

### 10.5 Recommendations

- Eligible intervention candidates.
- Priority and rationale.
- Evidence level.
- Expected effects when supportable.
- Production and operational risks.
- Reasons for blocked actions.

### 10.6 Verification and History

- Proposed actions.
- Approved actions.
- Completed actions.
- Actual post-intervention measurements.
- Verified outcomes and unresolved verification requirements.

The dashboard must display only outputs supported by the underlying pipeline. Illustrative mockup values must not be presented as real measurements.

## 11. Proposed Technology Stack

Initial technologies will be selected for feasibility and maintainability.

- **Programming language:** Python.
- **Data analysis:** Pandas and NumPy.
- **Machine learning:** scikit-learn; other models only when justified.
- **Model explanations:** suitable feature-importance methods, with SHAP if useful.
- **Visualisation:** Plotly.
- **Dashboard:** Streamlit.
- **Data storage:** CSV or Parquet for the initial data pipeline; SQLite if structured persistence is required.
- **Testing:** pytest.
- **Version control:** Git and GitHub.

An LLM may later assist with natural-language explanations, but it must not be responsible for unsupported engineering calculations or safety-critical decisions.

## 12. Team Responsibilities

### Project Manager and Analysis Lead

- Maintain the project scope and requirements.
- Coordinate industrial process understanding and dataset selection.
- Maintain the data dictionary and project documentation.
- Review assumptions, evidence and model results.
- Track dependencies, deadlines and technical blockers.
- Coordinate integration and final acceptance testing.

### Data Engineer

- Acquire and document datasets.
- Implement loading, validation and standardisation.
- Map source columns into the canonical schema.
- Maintain data provenance and data-quality reports.

### Data Analyst / ML Engineer

- Perform exploratory data analysis.
- Establish baselines and develop suitable models.
- Evaluate models using appropriate chronological validation.
- Document findings, limitations and model outputs.

### Decision Engine Developer

- Develop the intervention library.
- Implement action eligibility, constraints and the Evidence Gate.
- Implement scenario calculations and action ranking.
- Explain blocked recommendations and missing evidence.

### Dashboard / Integration Developer

- Build the Streamlit application.
- Connect validated data, model outputs and recommendations.
- Display trends, diagnostics, scenarios and action history.
- Handle missing data and error states.

### QA, Documentation and Demonstration

- Test module correctness and end-to-end behaviour.
- Validate numerical outputs, labels and constraints.
- Maintain setup instructions and test evidence.
- Prepare and rehearse the prototype demonstration.

Team members may perform multiple roles depending on team size. Every deliverable must nevertheless have one accountable owner.

## 13. Project Timeline

The internal prototype target is 10 November 2026.

| Period | Planned work | Milestone |
|---|---|---|
| 22–23 October | Freeze scope, assign owners, establish repository and requirements | Scope approved |
| 24–25 October | Acquire datasets, validate source structure and complete initial EDA | Data pipeline reproducible |
| 26–28 October | Establish baseline, detect deviations and evaluate modelling approach | First valid analytical finding |
| 29–31 October | Implement action library, Evidence Gate and constraints | Recommendation or blocking path works |
| 1–3 November | Implement scenario logic, ranking and history | Reproducible decision workflow |
| 4–5 November | Integrate core outputs into Streamlit | One complete UI workflow runs |
| 6–7 November | Conduct end-to-end testing and fix important defects | Core acceptance checks pass |
| 8–9 November | Rehearse and record prototype demonstration | Demo ready |
| 10 November | Final checks and submission | Prototype submitted |

The schedule is an internal execution plan. Optional features should be removed if required to protect the core workflow, testing and submission.

## 14. Testing and Acceptance Criteria

The prototype will be considered ready for demonstration when:

1. The source data can be loaded reproducibly.
2. The dataset's provenance, units and limitations are documented.
3. Data validation reports identify important data-quality issues.
4. The analytical pipeline produces reproducible findings.
5. Model performance is evaluated on appropriate unseen data where modelling is used.
6. Candidate interventions have explicit required inputs and constraints.
7. The Evidence Gate blocks unsupported actions.
8. Hard safety and production constraints cannot be overridden by ranking.
9. Any scenario calculation is reproducible and documents its assumptions.
10. Measured, derived and modelled values are clearly distinguished.
11. The dashboard displays outputs from the actual pipeline.
12. The system completes at least one end-to-end demonstration without fabricating measured results.
13. The application can be run using the documented setup instructions.
14. The demonstration clearly identifies limitations and future requirements.

## 15. Risks and Mitigation

| Risk | Mitigation |
|---|---|
| Actual textile-plant data is unavailable | Use a documented reference industrial dataset without claiming textile-specific validation |
| Required fuel, steam or production measurements are missing | Restrict the use case or block unsupported impact calculations |
| Emissions-monitor pollutant or units are unclear | Verify instrument documentation before using readings |
| Historical observations do not include intervention outcomes | Label simulations and plan future outcome verification |
| Correlation is mistaken for causation | Report likely associations rather than proven causes |
| Boiler or fan recommendations may violate operating constraints | Enforce approved limits and keep a qualified human in the decision loop |
| Team members develop incompatible modules | Agree on schemas, interfaces, task ownership and integration checkpoints |
| Scope expands beyond the deadline | Prioritise one complete end-to-end workflow |
| Dashboard values are invented to improve appearance | Require data provenance for every important metric |

## 16. Development and Collaboration Rules

- All project code and documentation must be committed to the shared GitHub repository.
- Team members should work on assigned branches and submit pull requests for review.
- The `main` branch should contain the most stable integrated version.
- Commits should have clear, descriptive messages.
- Changes affecting canonical fields or module interfaces must be documented.
- No passwords, access tokens, private credentials or unauthorised factory records may be committed.
- Raw datasets should not be uploaded without checking licence, size and confidentiality restrictions.
- New features must not be considered complete until their acceptance criteria have been tested.
- Blockers and scope changes should be raised during team check-ins.
- Project documentation should remain consistent with the actual implementation.

## 17. Future Development

After the initial prototype, potential extensions include:

- Additional textile equipment such as dyeing machines, stenters, pumps and compressors.
- More comprehensive production and utility integration.
- Additional facility configurations and industrial sectors.
- Improved intervention impact models using suitable real-world outcome data.
- Better uncertainty estimation and verified post-intervention analysis.
- Historical intervention learning.
- Integration with approved industrial monitoring systems.
- Additional regulatory and emissions-intensity decision-support features where applicable.

These extensions depend on data access, industrial validation and demonstrated feasibility.

## 18. Final Project Principle

CARBON-ACTION AI aims to connect industrial observations to practical, evidence-supported decisions.

The system must distinguish measured behaviour from inferred explanations and simulated outcomes. It should recommend only interventions supported by the necessary data and constraints, and it should explain when more evidence is needed.

**The objective is not merely to show how much energy or carbon a facility uses. It is to help determine what should be investigated or changed next, estimate the potential consequences responsibly, and verify what actually happened.**

