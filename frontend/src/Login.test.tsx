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
    expect(screen.getByText("Household name")).toBeInTheDocument();
  });
});
