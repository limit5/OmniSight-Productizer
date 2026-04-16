%% O7 (#270) — OmniSight Gerrit Prolog submit-rule.
%%
%% DUAL-+2 HARD GATE
%% ------------------
%% A change is only submittable when BOTH of these labels are set:
%%
%%   1.  Code-Review: +2 from a member of `non-ai-reviewer` (a HUMAN).
%%       No combination of AI votes can satisfy this — it is the
%%       hard gate.  Missing this ⇒ submit is ALWAYS rejected.
%%
%%   2.  Code-Review: +2 from a member of the `merger-agent-bot`
%%       account (the O6 Merger Agent).  Scope: the merger has
%%       simulated the merge and confirmed the conflict block
%%       resolves cleanly.  Missing this ⇒ submit is rejected EVEN IF
%%       a human +2 is present — the merger must co-sign the conflict.
%%
%% The rule also OR-propagates any -1 / -2 into a reject: a human's
%% negative vote blocks submission even if the Merger has +2'd.
%%
%% Group membership is checked via `gerrit:commit_author_in_group/1`
%% and `gerrit:user_in_group/1` — the latter works against the
%% label's voter identity.  We accept any +2 from the merger group,
%% not specifically one labelled `merger-agent-bot`, so adding more
%% merger bots in the future (e.g. per-language specialists) doesn't
%% require editing this rule.

submit_rule(S) :-
    gerrit:default_submit(X),
    X =.. [submit | Ls],
    require_human_plus_two(Ls, L1),
    require_merger_plus_two(L1, L2),
    reject_on_negative(L2, L3),
    S =.. [submit | L3].

%% ──────────────────────────────────────────────────────────────
%%  Require Code-Review: +2 from `non-ai-reviewer` (HUMAN).
%% ──────────────────────────────────────────────────────────────
require_human_plus_two(LabelsIn, LabelsOut) :-
    ( has_human_plus_two
    -> replace_label(LabelsIn, 'Code-Review', label('Code-Review', ok(_)), LabelsOut)
    ;  replace_label(LabelsIn, 'Code-Review',
                     label('Code-Review',
                           need(_, 'Needs +2 from non-ai-reviewer group (HUMAN hard gate)')),
                     LabelsOut)
    ).

has_human_plus_two :-
    gerrit:commit_label(label('Code-Review', 2), R),
    gerrit:user_in_group(R, 'non-ai-reviewer').

%% ──────────────────────────────────────────────────────────────
%%  Require Code-Review: +2 from the Merger Agent.
%%  Accepts any account in `ai-reviewer-bots` that ALSO belongs to
%%  the `merger-agent-bot` sub-group so future merger variants are
%%  automatically honoured.
%% ──────────────────────────────────────────────────────────────
require_merger_plus_two(LabelsIn, LabelsOut) :-
    ( has_merger_plus_two
    -> LabelsOut = LabelsIn
    ;  append(LabelsIn,
             [label('Merger-Signed',
                    need(_, 'Needs +2 from merger-agent-bot (conflict-block sign-off)'))],
             LabelsOut)
    ).

has_merger_plus_two :-
    gerrit:commit_label(label('Code-Review', 2), R),
    gerrit:user_in_group(R, 'merger-agent-bot').

%% ──────────────────────────────────────────────────────────────
%%  A single -1 / -2 from ANY reviewer blocks submission.
%% ──────────────────────────────────────────────────────────────
reject_on_negative(LabelsIn, LabelsOut) :-
    ( gerrit:commit_label(label('Code-Review', -1), _)
    ; gerrit:commit_label(label('Code-Review', -2), _)
    )
    -> replace_label(LabelsIn, 'Code-Review',
                     label('Code-Review',
                           reject(_, 'Reviewer cast a negative score')),
                     LabelsOut)
    ;  LabelsOut = LabelsIn.
reject_on_negative(Ls, Ls).

%% ──────────────────────────────────────────────────────────────
%%  Utility: replace the named label inside the default submit term.
%% ──────────────────────────────────────────────────────────────
replace_label([], _, _, []).
replace_label([label(Name, _Old) | Rest], Name, New, [New | Rest]) :- !.
replace_label([Head | Rest], Name, New, [Head | Rest2]) :-
    replace_label(Rest, Name, New, Rest2).
