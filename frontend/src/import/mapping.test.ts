import { describe, expect, it } from "vitest";

import type { CsvMapping } from "../api/types";
import { mappingFromRoles, rolesFromMapping, whatsMissing, type Roles } from "./mapping";

/**
 * Roles are what the mapping step edits and the API mapping is derived from them, so
 * these are the rules that decide whether a file is read correctly at all.
 */

const BASE: CsvMapping = {
  has_header: true,
  date_col: 0,
  description_col: 1,
  amount_mode: "single",
  amount_col: 2,
  debit_col: null,
  credit_col: null,
  balance_col: null,
  date_format: null,
  dayfirst: true,
  decimal: ".",
  invert_amount: false,
  skip_rows: 0,
  account_col: null,
  delimiter: ",",
};

// Bank Account, Date, Narrative, Debit Amount, Credit Amount, Balance, Categories, Serial
const WESTPAC: CsvMapping = {
  ...BASE,
  date_col: 1,
  description_col: 2,
  amount_mode: "debit_credit",
  amount_col: null,
  debit_col: 3,
  credit_col: 4,
  balance_col: 5,
  account_col: 0,
};

describe("rolesFromMapping", () => {
  it("gives every column a role, so none is silently unaccounted for", () => {
    const roles = rolesFromMapping(WESTPAC, 8);
    expect(roles).toEqual({
      0: "account",
      1: "date",
      2: "description",
      3: "money_out",
      4: "money_in",
      5: "balance",
      6: "ignore",
      7: "ignore",
    });
  });

  it("shows a single signed column as Amount rather than as a debit", () => {
    expect(rolesFromMapping(BASE, 3)[2]).toBe("amount");
  });

  it("ignores a column index the file does not have", () => {
    // A remembered mapping can outlive the shape it was made for.
    expect(rolesFromMapping({ ...BASE, balance_col: 99 }, 3)[2]).toBe("amount");
  });
});

describe("mappingFromRoles", () => {
  it("round-trips a real statement", () => {
    const roles = rolesFromMapping(WESTPAC, 8);
    expect(mappingFromRoles(roles, BASE)).toMatchObject({
      date_col: 1,
      description_col: 2,
      account_col: 0,
      balance_col: 5,
      amount_mode: "debit_credit",
      debit_col: 3,
      credit_col: 4,
      amount_col: null,
    });
  });

  it("keeps settings the roles say nothing about", () => {
    const roles = rolesFromMapping(BASE, 3);
    const base = { ...BASE, delimiter: "\t", has_header: false, date_format: "%d/%m/%Y" };
    expect(mappingFromRoles(roles, base)).toMatchObject({
      delimiter: "\t",
      has_header: false,
      date_format: "%d/%m/%Y",
    });
  });

  it("drops the account column when the role is taken off it", () => {
    const roles: Roles = { ...rolesFromMapping(WESTPAC, 8), 0: "ignore" };
    expect(mappingFromRoles(roles, BASE).account_col).toBeNull();
  });
});

describe("whatsMissing", () => {
  const ok: Roles = { 0: "date", 1: "description", 2: "amount" };

  it("says nothing when the file can be read", () => {
    expect(whatsMissing(ok)).toBeNull();
    expect(whatsMissing({ 0: "date", 1: "description", 2: "money_out" })).toBeNull();
  });

  it("names the missing piece rather than leaving a dead button", () => {
    expect(whatsMissing({ 1: "description", 2: "amount" })).toMatch(/date/i);
    expect(whatsMissing({ 0: "date", 2: "amount" })).toMatch(/describes/i);
    expect(whatsMissing({ 0: "date", 1: "description" })).toMatch(/amount/i);
  });

  it("refuses a signed amount alongside separate in and out columns", () => {
    // The same money described twice; which one wins would be anyone's guess.
    expect(whatsMissing({ ...ok, 3: "money_out" })).toMatch(/either/i);
  });

  it("refuses two columns claiming the same role", () => {
    expect(whatsMissing({ ...ok, 3: "date" })).toMatch(/only one/i);
  });

  it("does not object to several ignored columns", () => {
    expect(whatsMissing({ ...ok, 3: "ignore", 4: "ignore", 5: "ignore" })).toBeNull();
  });
});
