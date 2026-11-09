// SPDX-License-Identifier: MIT
// Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
// Page shell wiring shared by every server-rendered page: theme toggle and
// the system-status dialog.

import { setUpThemeToggle } from "./theme.js";
import { setUpSystemStatusDialog } from "./system-status.js";
import { installHelp } from "./help.js";

setUpThemeToggle("theme-toggle");
setUpSystemStatusDialog();
// Contextual help for the shell (Help link, system status, theme). Pages
// with their own module (project, workspace) call installHelp again after
// rendering dynamic content; the registry is fetched once.
if (!document.getElementById("project-body") && !document.getElementById("annotation-body")) installHelp(document);
