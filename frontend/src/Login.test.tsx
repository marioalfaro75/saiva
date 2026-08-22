import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AuthProvider } from "./auth/AuthContext";
import { Login } from "./pages/Login";

describe("Login", () => {
  it("renders the create-household form on first run", () => {
    render(
      <AuthProvider>
        <Login initialised={false} />
      </AuthProvider>,
    );
    expect(screen.getByRole("button", { name: "Create household" })).toBeInTheDocument();
    expect(screen.getByLabelText("Household name")).toBeInTheDocument();
  });

  /**
   * Every label on this form used to be text sitting next to a box with nothing
   * joining them, so a screen reader announced both credential fields as an unnamed
   * "edit text" and tapping the word did not focus the field.
   */
  it("names every field, so the form can be used without seeing it", () => {
    render(
      <AuthProvider>
        <Login initialised={true} />
      </AuthProvider>,
    );
    for (const name of ["Email", "Password"]) {
      expect(screen.getByLabelText(name)).toBeInTheDocument();
    }
  });

  it("gives the first-run fields names too", () => {
    render(
      <AuthProvider>
        <Login initialised={false} />
      </AuthProvider>,
    );
    for (const name of ["Household name", "Your name", "Budget period", "Email", "Password"]) {
      expect(screen.getByLabelText(name)).toBeInTheDocument();
    }
  });
});
