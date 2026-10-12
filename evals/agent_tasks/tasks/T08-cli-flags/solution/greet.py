import argparse


def positive_int(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return n


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--shout", action="store_true")
    parser.add_argument("--times", type=positive_int, default=1)
    args = parser.parse_args(argv)
    line = f"Hello, {args.name}!"
    if args.shout:
        line = line.upper()
    for _ in range(args.times):
        print(line)


if __name__ == "__main__":
    main()
