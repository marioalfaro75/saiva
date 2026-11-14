import { CategoriesManager } from "../components/CategoriesManager";
import { PageHead } from "../components/PageHead";

/** Add, rename and remove the categories transactions are sorted into. */
export function Categories() {
  return (
    <div>
      <PageHead title="Categories" />
      <CategoriesManager />
    </div>
  );
}
