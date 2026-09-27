package calc

import "testing"

func TestClamp(t *testing.T) {
	if Clamp(5, 0, 3) != 3 || Clamp(-1, 0, 3) != 0 {
		t.Fatal("clamp")
	}
}

func TestPercentBasic(t *testing.T) {
	if Percent(1, 4) != 25.0 {
		t.Fatal("percent")
	}
}

func TestAddReturnsSum(t *testing.T) {
	t.Run("small", func(t *testing.T) {
		if Add(2, 3) != 5 {
			t.Fatalf("add(2, 3) = %d", Add(2, 3))
		}
	})
}

func TestSkipped(t *testing.T) {
	t.Skip("placeholder")
}
